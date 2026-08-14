"""Run-wide budgets, no-progress diagnostics, and late-result recording."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from app.models.common import utcnow, uuid7
from app.models.foundation import UserSession
from app.models.runtime_v1 import (
    AgentInvocation, AgentTurn, Approval, Clarification, DurableRunner,
    ProviderAttempt, Run, ToolAttempt, ToolInvocation, WorkItem,
)
from app.models.verification_v1 import LateResultObservation, RunProgressMark
from app.persistence.reliable_writes import ExecutionEventWriter
from app.services.run_followup_service import enqueue_run_terminal_followups


TERMINAL_RUN_STATUSES = {"completed", "cancelled", "failed"}


class BudgetExceeded(RuntimeError):
    code = "budget_exhausted"

    def __init__(self, dimension: str, *, limit: int | float, used: int | float):
        self.dimension = dimension
        self.limit = limit
        self.used = used
        super().__init__(f"Run budget exhausted: {dimension} ({used}/{limit})")


@dataclass(frozen=True)
class RunBudgetState:
    limits: dict[str, int | float]
    usage: dict[str, int | float]
    remaining_transport_retries: int
    warning_dimensions: tuple[str, ...]


def default_run_budget(mode: str) -> dict[str, int]:
    """Bound every Run while leaving enough room for real peer collaboration."""

    if mode == "group":
        return {
            "max_agent_turns": 160,
            "max_provider_attempts": 240,
            "max_tool_invocations": 128,
            "max_agent_invocations": 64,
            "max_agent_depth": 8,
            "max_agent_fan_out_per_work_item": 8,
            "max_transport_retries": 16,
            "max_tokens": 768_000,
            "max_active_seconds": 1_800,
            "no_progress_turns": 4,
        }
    return {
        "max_agent_turns": 64,
        "max_provider_attempts": 96,
        "max_tool_invocations": 64,
        "max_agent_invocations": 24,
        "max_agent_depth": 1,
        "max_agent_fan_out_per_work_item": 8,
        "max_transport_retries": 8,
        # A real-model Plan implementation can legitimately span roughly 20
        # turns when every durable Tool result resumes through a new Runner.
        # Keep enough variance headroom above the observed ~162k peak while
        # retaining a strict, immutable Run-wide cap.
        "max_tokens": 200_000,
        "max_active_seconds": 900,
        "no_progress_turns": 4,
    }


def normalize_run_budget(raw: dict[str, Any] | None, *, mode: str) -> dict[str, int | float]:
    defaults = default_run_budget(mode)
    supplied = dict(raw or {})
    # The first Group vertical used max_turns. Keep reading it as an alias while
    # all new writes use the unambiguous Run-wide name.
    if "max_turns" in supplied and "max_agent_turns" not in supplied:
        supplied["max_agent_turns"] = supplied["max_turns"]
    normalized: dict[str, int | float] = {}
    for key, fallback in defaults.items():
        value = supplied.get(key, fallback)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            value = fallback
        normalized[key] = value
    return normalized


async def load_run_budget_state(db, run_id: str) -> RunBudgetState:
    run = await db.get(Run, run_id)
    if run is None:
        raise LookupError(f"run not found: {run_id}")
    turns = int(await db.scalar(select(func.count(AgentTurn.id)).where(
        AgentTurn.run_id == run_id,
    )) or 0)
    attempt_rows = list(await db.scalars(
        select(ProviderAttempt).join(
            AgentTurn, AgentTurn.id == ProviderAttempt.agent_turn_id,
        ).where(AgentTurn.run_id == run_id)
    ))
    provider_attempts = len(attempt_rows)
    attempted_turns = len({item.agent_turn_id for item in attempt_rows})
    transport_retries = max(0, provider_attempts - attempted_turns)
    tokens = 0
    active_seconds = 0.0
    for attempt in attempt_rows:
        usage = dict(attempt.usage or {})
        tokens += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        if attempt.started_at and attempt.ended_at:
            active_seconds += max(0.0, (attempt.ended_at - attempt.started_at).total_seconds())
    tool_invocations = int(await db.scalar(select(func.count(ToolInvocation.id)).where(
        ToolInvocation.run_id == run_id,
    )) or 0)
    agent_invocations = int(await db.scalar(select(func.count(AgentInvocation.id)).where(
        AgentInvocation.run_id == run_id,
    )) or 0)
    usage: dict[str, int | float] = {
        "agent_turns": turns,
        "provider_attempts": provider_attempts,
        "tool_invocations": tool_invocations,
        "agent_invocations": agent_invocations,
        "transport_retries": transport_retries,
        "tokens": tokens,
        "active_seconds": round(active_seconds, 6),
    }
    limits = normalize_run_budget(run.hard_budget_snapshot, mode=run.mode)
    dimensions = {
        "agent_turns": "max_agent_turns",
        "provider_attempts": "max_provider_attempts",
        "tool_invocations": "max_tool_invocations",
        "agent_invocations": "max_agent_invocations",
        "transport_retries": "max_transport_retries",
        "tokens": "max_tokens",
        "active_seconds": "max_active_seconds",
    }
    warnings = tuple(
        dimension for dimension, limit_key in dimensions.items()
        if float(usage[dimension]) >= float(limits[limit_key]) * 0.8
    )
    return RunBudgetState(
        limits=limits,
        usage=usage,
        remaining_transport_retries=max(
            0, int(limits["max_transport_retries"]) - transport_retries,
        ),
        warning_dimensions=warnings,
    )


def _assert_dimension(state: RunBudgetState, dimension: str, limit_key: str) -> None:
    if float(state.usage[dimension]) >= float(state.limits[limit_key]):
        raise BudgetExceeded(
            dimension, limit=state.limits[limit_key], used=state.usage[dimension],
        )


async def assert_turn_budget(db, run_id: str) -> RunBudgetState:
    state = await load_run_budget_state(db, run_id)
    _assert_dimension(state, "agent_turns", "max_agent_turns")
    _assert_dimension(state, "provider_attempts", "max_provider_attempts")
    _assert_dimension(state, "tokens", "max_tokens")
    _assert_dimension(state, "active_seconds", "max_active_seconds")
    return state


async def assert_post_provider_budget(db, run_id: str) -> RunBudgetState:
    """Reject non-token overage observed after a provider response arrived.

    Token usage is authoritative only after the provider returns.  A Turn that
    started below the token limit is therefore allowed to finish and consume
    its complete result even when that result crosses the limit.  The next
    provider call is rejected by ``assert_turn_budget`` using the persisted
    actual usage.  Other provider/time limits retain their immediate guard.
    """

    state = await load_run_budget_state(db, run_id)
    for dimension, limit_key in (
        ("provider_attempts", "max_provider_attempts"),
        ("transport_retries", "max_transport_retries"),
        ("active_seconds", "max_active_seconds"),
    ):
        if float(state.usage[dimension]) > float(state.limits[limit_key]):
            raise BudgetExceeded(
                dimension, limit=state.limits[limit_key], used=state.usage[dimension],
            )
    return state


async def assert_action_budget(db, run_id: str, *, action_kind: str) -> RunBudgetState:
    state = await load_run_budget_state(db, run_id)
    if action_kind == "tool":
        _assert_dimension(state, "tool_invocations", "max_tool_invocations")
    elif action_kind == "agent":
        _assert_dimension(state, "agent_invocations", "max_agent_invocations")
    return state


async def record_progress(
    db, *, run_id: str, work_item_id: str, runner_id: str,
    agent_turn_id: str, made_progress: bool, payload: dict[str, Any],
    reason_code: str,
) -> RunProgressMark:
    existing = await db.scalar(select(RunProgressMark).where(
        RunProgressMark.agent_turn_id == agent_turn_id,
    ))
    if existing is not None:
        return existing
    previous = await db.scalar(select(RunProgressMark).where(
        RunProgressMark.run_id == run_id,
    ).order_by(RunProgressMark.created_at.desc(), RunProgressMark.id.desc()).limit(1))
    consecutive = 0 if made_progress else (
        int(previous.consecutive_no_progress) + 1 if previous is not None else 1
    )
    fingerprint = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    mark = RunProgressMark(
        id=uuid7(), run_id=run_id, work_item_id=work_item_id,
        runner_id=runner_id, agent_turn_id=agent_turn_id,
        made_progress=made_progress, progress_fingerprint=fingerprint,
        reason_code=reason_code, consecutive_no_progress=consecutive,
    )
    db.add(mark)
    await db.flush()
    return mark


def _safe_late_result(result: dict[str, Any]) -> dict[str, Any]:
    """Retain diagnosis without copying unbounded stdout/stderr into an Event."""

    error = result.get("error") if isinstance(result.get("error"), dict) else None
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return {
        "status": result.get("status"),
        "error_code": error.get("code") if error else None,
        "provider_request_id": result.get("provider_request_id"),
        "exit_code": data.get("exit_code"),
        "timed_out": data.get("timed_out"),
        "stdout_sha256": hashlib.sha256(str(data.get("stdout") or "").encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(str(data.get("stderr") or "").encode()).hexdigest(),
    }


async def record_late_result(
    db, *, run: Run, work_item_id: str | None, source_type: str,
    source_id: str, original_status: str, result: dict[str, Any], actor_id: str,
) -> LateResultObservation:
    safe = _safe_late_result(result)
    result_hash = hashlib.sha256(json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    existing = await db.scalar(select(LateResultObservation).where(
        LateResultObservation.source_type == source_type,
        LateResultObservation.source_id == source_id,
        LateResultObservation.result_hash == result_hash,
    ))
    if existing is not None:
        return existing
    # Event sequence allocation and the observation share one transaction.
    await db.scalar(select(UserSession).where(
        UserSession.id == run.session_id,
    ).execution_options(populate_existing=True).with_for_update())
    observation = LateResultObservation(
        id=uuid7(), run_id=run.id, work_item_id=work_item_id,
        source_type=source_type, source_id=source_id,
        original_status=original_status, result_hash=result_hash,
        safe_result=safe,
    )
    db.add(observation)
    await db.flush()
    await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id,
        event_type="late_result.observed",
        event_dedupe_key=f"late-result:{source_type}:{source_id}:{result_hash}",
        actor_type="service", actor_id=actor_id,
        entity_type="late_result", entity_id=observation.id,
        visibility="user", summary="A late result was recorded without reactivating the Run",
        payload={
            "source_type": source_type, "source_id": source_id,
            "original_status": original_status,
        },
        correlation_id=run.id,
    )
    return observation


def deadline_from_budget(raw: dict[str, Any] | None, *, mode: str):
    budget = normalize_run_budget(raw, mode=mode)
    return utcnow() + timedelta(seconds=int(budget["max_active_seconds"]) * 4)


async def expire_one_run_deadline(db, *, actor_id: str) -> bool:
    """Terminalize one expired Run and fence every outstanding action.

    This is a persisted scheduler operation, so a worker restart merely delays
    the scan. It never depends on an in-process timer.
    """

    candidate = await db.scalar(select(Run).where(
        Run.status.not_in(TERMINAL_RUN_STATUSES),
        Run.deadline_at.is_not(None),
        Run.deadline_at <= utcnow(),
    ).order_by(Run.deadline_at, Run.id).limit(1))
    if candidate is None:
        return False
    await db.scalar(select(UserSession).where(
        UserSession.id == candidate.session_id,
    ).execution_options(populate_existing=True).with_for_update())
    run = await db.scalar(select(Run).where(
        Run.id == candidate.id,
    ).execution_options(populate_existing=True).with_for_update())
    now = utcnow()
    if (
        run is None or run.status in TERMINAL_RUN_STATUSES
        or run.deadline_at is None or run.deadline_at > now
    ):
        return False

    runners = list(await db.scalars(select(DurableRunner).where(
        DurableRunner.run_id == run.id,
        DurableRunner.status.in_(("queued", "running")),
    ).with_for_update()))
    for runner in runners:
        runner.status = "cancelled"
        runner.failure_code = "run_deadline_exceeded"
        runner.error_code = "run_deadline_exceeded"
        runner.error_detail = "Run hard deadline elapsed"
        runner.lease_expires_at = None
        runner.ended_at = now
        runner.version += 1

    invocations = list(await db.scalars(select(ToolInvocation).where(
        ToolInvocation.run_id == run.id,
        ToolInvocation.status.in_((
            "created", "waiting_confirmation", "waiting_resource", "queued",
            "running", "cancel_requested",
        )),
    ).with_for_update()))
    for invocation in invocations:
        attempts = list(await db.scalars(select(ToolAttempt).where(
            ToolAttempt.tool_invocation_id == invocation.id,
            ToolAttempt.status.in_(("queued", "running")),
        ).with_for_update()))
        uncertain = False
        for attempt in attempts:
            uncertain = uncertain or attempt.side_effect_started
            attempt.status = "outcome_unknown" if attempt.side_effect_started else "timed_out"
            attempt.error_code = "run_deadline_exceeded"
            attempt.error_detail = "Run hard deadline elapsed"
            attempt.outcome_confirmed = not attempt.side_effect_started
            attempt.lease_expires_at = None
            attempt.ended_at = now
        invocation.status = "outcome_unknown" if uncertain else "failed"
        invocation.error_code = "run_deadline_exceeded"
        invocation.error_detail = "Run hard deadline elapsed"
        invocation.result_data = {
            "status": "failed",
            "summary": "Run hard deadline elapsed",
            "data": None,
            "error": {
                "code": "run_deadline_exceeded",
                "message": "Run hard deadline elapsed",
                "category": "outcome_unknown" if uncertain else "provider",
                "details": {},
            },
        }
        invocation.version += 1

    agent_invocations = list(await db.scalars(select(AgentInvocation).where(
        AgentInvocation.run_id == run.id,
        AgentInvocation.status.in_(("created", "queued", "running")),
    ).with_for_update()))
    for invocation in agent_invocations:
        invocation.status = "timed_out"
        invocation.error_code = "run_deadline_exceeded"
        invocation.error_detail = "Run hard deadline elapsed"
        invocation.completed_at = now
        invocation.version += 1

    approvals = list(await db.scalars(select(Approval).where(
        Approval.run_id == run.id, Approval.status == "pending",
    ).with_for_update()))
    for approval in approvals:
        approval.status = "cancelled"
        approval.decision_reason = "Run hard deadline elapsed"
        approval.decided_at = now
        approval.version += 1
    clarifications = list(await db.scalars(select(Clarification).where(
        Clarification.run_id == run.id, Clarification.status == "open",
    ).with_for_update()))
    for clarification in clarifications:
        clarification.status = "cancelled"
        clarification.version += 1

    work_items = list(await db.scalars(select(WorkItem).where(
        WorkItem.run_id == run.id,
        WorkItem.status.in_((
            "queued", "running", "waiting_user", "waiting_approval",
            "waiting_agent", "waiting_tool", "waiting_resource",
        )),
    ).with_for_update()))
    for work in work_items:
        work.status = "timed_out"
        work.wait_reason = None
        work.error_code = "run_deadline_exceeded"
        work.error_detail = "Run hard deadline elapsed"
        work.active_runner_id = None
        work.version += 1

    run.status = "failed"
    run.wait_reason = None
    run.terminal_reason = "run_deadline_exceeded"
    run.completed_at = now
    run.version += 1
    failed = await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id,
        event_type="run.failed",
        event_dedupe_key=f"run:{run.id}:deadline:{run.deadline_at.isoformat()}",
        actor_type="service", actor_id=actor_id,
        entity_type="run", entity_id=run.id, entity_version=run.version,
        visibility="user", summary="Run failed because its hard deadline elapsed",
        payload={
            "error_code": "run_deadline_exceeded",
            "deadline_at": run.deadline_at.isoformat(),
            "tool_outcomes_unknown": sum(
                1 for item in invocations if item.status == "outcome_unknown"
            ),
        },
        correlation_id=run.id,
    )
    followup_runner = await db.scalar(select(DurableRunner).where(
        DurableRunner.run_id == run.id,
        *(
            (DurableRunner.work_item_id == run.root_work_item_id,)
            if run.root_work_item_id else ()
        ),
    ).order_by(DurableRunner.created_at.desc(), DurableRunner.id.desc()).limit(1))
    await enqueue_run_terminal_followups(
        db, run=run, runner=followup_runner, completion_event=failed,
    )
    return True
