"""Read-only, redacted operational projections for the Runtime console."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import utcnow
from app.models.foundation import ExecutionEvent, OutboxCommand, UserSession
from app.models.group_v1 import GroupDiagnostic
from app.models.runtime_v1 import (
    AgentInvocation,
    AgentTurn,
    DurableRunner,
    ProviderAttempt,
    Run,
    ToolAttempt,
    ToolInvocation,
    WorkItem,
)
from app.models.verification_v1 import RunProgressMark
from app.services.group_diagnostic_service import group_diagnostic_view
from app.services.run_reliability_service import normalize_run_budget


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _age_seconds(value: datetime | None, *, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, int((now - value).total_seconds()))


async def _grouped_counts(
    db: AsyncSession, model, key_column, status_column, run_ids: list[str],
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = defaultdict(dict)
    if not run_ids:
        return result
    rows = (await db.execute(select(
        key_column, status_column, func.count(),
    ).where(key_column.in_(run_ids)).group_by(key_column, status_column))).all()
    for run_id, status, count in rows:
        result[str(run_id)][str(status)] = int(count)
    return result


async def project_runtime_runs(
    db: AsyncSession, runs: list[Run],
) -> list[dict[str, Any]]:
    """Batch-project operational Run facts without loading prompt or result text."""

    if not runs:
        return []
    now = utcnow()
    run_ids = [item.id for item in runs]
    session_ids = {item.session_id for item in runs}
    account_by_session = dict((await db.execute(select(
        UserSession.id, UserSession.account_id,
    ).where(UserSession.id.in_(session_ids)))).all())

    work_counts = await _grouped_counts(
        db, WorkItem, WorkItem.run_id, WorkItem.status, run_ids,
    )
    runner_counts = await _grouped_counts(
        db, DurableRunner, DurableRunner.run_id, DurableRunner.status, run_ids,
    )
    tool_counts = await _grouped_counts(
        db, ToolInvocation, ToolInvocation.run_id, ToolInvocation.status, run_ids,
    )
    agent_counts = await _grouped_counts(
        db, AgentInvocation, AgentInvocation.run_id, AgentInvocation.status, run_ids,
    )
    turn_counts = dict((await db.execute(select(
        AgentTurn.run_id, func.count(AgentTurn.id),
    ).where(AgentTurn.run_id.in_(run_ids)).group_by(AgentTurn.run_id))).all())
    diagnostic_counts = dict((await db.execute(select(
        GroupDiagnostic.run_id, func.count(GroupDiagnostic.id),
    ).where(GroupDiagnostic.run_id.in_(run_ids)).group_by(
        GroupDiagnostic.run_id,
    ))).all())

    usage_by_run: dict[str, dict[str, int | float]] = defaultdict(lambda: {
        "agent_turns": 0,
        "provider_attempts": 0,
        "tool_invocations": 0,
        "agent_invocations": 0,
        "transport_retries": 0,
        "tokens": 0,
        "active_seconds": 0.0,
    })
    attempted_turns_by_run: dict[str, set[str]] = defaultdict(set)
    attempt_rows = (await db.execute(select(
        AgentTurn.run_id,
        ProviderAttempt.agent_turn_id,
        ProviderAttempt.usage,
        ProviderAttempt.started_at,
        ProviderAttempt.ended_at,
    ).join(
        AgentTurn, AgentTurn.id == ProviderAttempt.agent_turn_id,
    ).where(AgentTurn.run_id.in_(run_ids)))).all()
    for run_id, turn_id, usage, started_at, ended_at in attempt_rows:
        current = usage_by_run[str(run_id)]
        current["provider_attempts"] = int(current["provider_attempts"]) + 1
        attempted_turns_by_run[str(run_id)].add(str(turn_id))
        values = dict(usage or {})
        current["tokens"] = int(current["tokens"]) + int(
            values.get("input_tokens") or 0
        ) + int(values.get("output_tokens") or 0)
        if started_at and ended_at:
            current["active_seconds"] = float(current["active_seconds"]) + max(
                0.0, (ended_at - started_at).total_seconds(),
            )

    progress_by_run: dict[str, RunProgressMark] = {}
    progress_rows = list(await db.scalars(select(RunProgressMark).where(
        RunProgressMark.run_id.in_(run_ids),
    ).order_by(
        RunProgressMark.created_at.desc(), RunProgressMark.id.desc(),
    )))
    for row in progress_rows:
        progress_by_run.setdefault(row.run_id, row)

    items = []
    dimensions = {
        "agent_turns": "max_agent_turns",
        "provider_attempts": "max_provider_attempts",
        "tool_invocations": "max_tool_invocations",
        "agent_invocations": "max_agent_invocations",
        "transport_retries": "max_transport_retries",
        "tokens": "max_tokens",
        "active_seconds": "max_active_seconds",
    }
    for run in runs:
        usage = usage_by_run[run.id]
        usage["agent_turns"] = int(turn_counts.get(run.id, 0))
        usage["tool_invocations"] = sum(tool_counts.get(run.id, {}).values())
        usage["agent_invocations"] = sum(agent_counts.get(run.id, {}).values())
        usage["transport_retries"] = max(
            0,
            int(usage["provider_attempts"]) - len(attempted_turns_by_run[run.id]),
        )
        usage["active_seconds"] = round(float(usage["active_seconds"]), 6)
        limits = normalize_run_budget(run.hard_budget_snapshot, mode=run.mode)
        warning_dimensions = [
            dimension for dimension, limit_key in dimensions.items()
            if float(usage[dimension]) >= float(limits[limit_key]) * 0.8
        ]
        progress = progress_by_run.get(run.id)
        items.append({
            "id": run.id,
            "session_id": run.session_id,
            "account_id": account_by_session.get(run.session_id),
            "mode": run.mode,
            "status": run.status,
            "wait_reason": run.wait_reason,
            "terminal_reason": run.terminal_reason,
            "root_work_item_id": run.root_work_item_id,
            "permission_mode": run.permission_mode,
            "created_at": _iso(run.created_at),
            "updated_at": _iso(run.updated_at),
            "completed_at": _iso(run.completed_at),
            "deadline_at": _iso(run.deadline_at),
            "age_seconds": _age_seconds(run.created_at, now=now),
            "counts": {
                "work_items": work_counts.get(run.id, {}),
                "runners": runner_counts.get(run.id, {}),
                "tool_invocations": tool_counts.get(run.id, {}),
                "agent_invocations": agent_counts.get(run.id, {}),
                "diagnostics": int(diagnostic_counts.get(run.id, 0)),
            },
            "budget": {
                "limits": limits,
                "usage": dict(usage),
                "warning_dimensions": warning_dimensions,
            },
            "last_progress": None if progress is None else {
                "made_progress": progress.made_progress,
                "reason_code": progress.reason_code,
                "consecutive_no_progress": progress.consecutive_no_progress,
                "created_at": _iso(progress.created_at),
            },
        })
    return items


async def runtime_console_summary(db: AsyncSession) -> dict[str, Any]:
    now = utcnow()

    async def status_counts(model, status_column) -> dict[str, int]:
        rows = (await db.execute(select(
            status_column, func.count(),
        ).group_by(status_column))).all()
        return {str(status): int(count) for status, count in rows}

    run_counts = await status_counts(Run, Run.status)
    runner_counts = await status_counts(DurableRunner, DurableRunner.status)
    tool_counts = await status_counts(ToolInvocation, ToolInvocation.status)
    agent_counts = await status_counts(AgentInvocation, AgentInvocation.status)
    outbox_rows = (await db.execute(select(
        OutboxCommand.topic, OutboxCommand.status, func.count(),
        func.min(OutboxCommand.created_at),
    ).group_by(OutboxCommand.topic, OutboxCommand.status))).all()
    outbox = [{
        "topic": topic,
        "status": status,
        "count": int(count),
        "oldest_age_seconds": _age_seconds(oldest, now=now),
    } for topic, status, count, oldest in outbox_rows]

    workers: dict[tuple[str, str], dict[str, Any]] = {}
    runner_leases = (await db.execute(select(
        DurableRunner.lease_owner,
        DurableRunner.heartbeat_at,
        DurableRunner.lease_expires_at,
    ).where(
        DurableRunner.status == "running",
        DurableRunner.lease_owner.is_not(None),
    ))).all()
    tool_leases = (await db.execute(select(
        ToolAttempt.worker_id,
        ToolAttempt.heartbeat_at,
        ToolAttempt.lease_expires_at,
    ).where(
        ToolAttempt.status == "running",
        ToolAttempt.worker_id.is_not(None),
    ))).all()
    claimed_outbox = (await db.execute(select(
        OutboxCommand.dispatch_lease_owner,
        OutboxCommand.updated_at,
        OutboxCommand.dispatch_lease_expires_at,
    ).where(
        OutboxCommand.status == "claimed",
        OutboxCommand.dispatch_lease_owner.is_not(None),
    ))).all()
    for kind, rows in (
        ("runtime", runner_leases),
        ("tool", tool_leases),
        ("outbox", claimed_outbox),
    ):
        for worker_id, heartbeat_at, lease_expires_at in rows:
            key = (kind, str(worker_id))
            item = workers.setdefault(key, {
                "worker_kind": kind,
                "worker_id": str(worker_id),
                "observed_active_slots": 0,
                "last_heartbeat_at": None,
                "stale_leases": 0,
            })
            item["observed_active_slots"] += 1
            if heartbeat_at and (
                item["last_heartbeat_at"] is None
                or heartbeat_at > item["last_heartbeat_at"]
            ):
                item["last_heartbeat_at"] = heartbeat_at
            if lease_expires_at is None or lease_expires_at <= now:
                item["stale_leases"] += 1
    worker_items = []
    for item in workers.values():
        item["health"] = "stale" if item["stale_leases"] else "active"
        item["last_heartbeat_at"] = _iso(item["last_heartbeat_at"])
        worker_items.append(item)
    worker_items.sort(key=lambda item: (item["worker_kind"], item["worker_id"]))

    return {
        "generated_at": _iso(now),
        "runs": run_counts,
        "runners": runner_counts,
        "tool_invocations": tool_counts,
        "agent_invocations": agent_counts,
        "outbox": outbox,
        "observed_workers": worker_items,
        "outcome_unknown": {
            "provider_attempts": int(await db.scalar(select(
                func.count(ProviderAttempt.id),
            ).where(ProviderAttempt.status == "outcome_unknown")) or 0),
            "tool_attempts": int(await db.scalar(select(
                func.count(ToolAttempt.id),
            ).where(ToolAttempt.status == "outcome_unknown")) or 0),
        },
        "diagnostic_count": int(await db.scalar(select(
            func.count(GroupDiagnostic.id),
        )) or 0),
    }


async def runtime_run_detail(db: AsyncSession, run: Run) -> dict[str, Any]:
    projection = (await project_runtime_runs(db, [run]))[0]
    work_items = list(await db.scalars(select(WorkItem).where(
        WorkItem.run_id == run.id,
    ).order_by(WorkItem.created_at, WorkItem.id)))
    runners = list(await db.scalars(select(DurableRunner).where(
        DurableRunner.run_id == run.id,
    ).order_by(DurableRunner.created_at, DurableRunner.id)))
    tool_invocations = list(await db.scalars(select(ToolInvocation).where(
        ToolInvocation.run_id == run.id,
    ).order_by(ToolInvocation.created_at, ToolInvocation.id)))
    agent_invocations = list(await db.scalars(select(AgentInvocation).where(
        AgentInvocation.run_id == run.id,
    ).order_by(AgentInvocation.created_at, AgentInvocation.id)))
    provider_attempts = (await db.execute(select(
        ProviderAttempt, AgentTurn.runner_id,
    ).join(
        AgentTurn, AgentTurn.id == ProviderAttempt.agent_turn_id,
    ).where(AgentTurn.run_id == run.id).order_by(
        ProviderAttempt.created_at, ProviderAttempt.id,
    ))).all()
    diagnostics = list(await db.scalars(select(GroupDiagnostic).where(
        GroupDiagnostic.run_id == run.id,
    ).order_by(GroupDiagnostic.created_at.desc(), GroupDiagnostic.id.desc())))
    events = list(await db.scalars(select(ExecutionEvent).where(
        ExecutionEvent.run_id == run.id,
    ).order_by(ExecutionEvent.sequence.desc()).limit(200)))

    related_ids = {
        run.id,
        *(item.id for item in work_items),
        *(item.id for item in runners),
        *(item.id for item in tool_invocations),
        *(item.id for item in agent_invocations),
    }
    outbox = list(await db.scalars(select(OutboxCommand).where(
        OutboxCommand.aggregate_id.in_(related_ids),
    ).order_by(OutboxCommand.created_at.desc()).limit(200)))

    return {
        **projection,
        "work_items": [{
            "id": item.id,
            "owner_agent_id": item.owner_agent_id,
            "parent_work_item_id": item.parent_work_item_id,
            "purpose": item.purpose,
            "status": item.status,
            "wait_reason": item.wait_reason,
            "active_runner_id": item.active_runner_id,
            "execution_epoch": item.execution_epoch,
            "attempt_no": item.attempt_no,
            "retryable": item.retryable,
            "error_code": item.error_code,
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
        } for item in work_items],
        "runners": [{
            "id": item.id,
            "work_item_id": item.work_item_id,
            "agent_id": item.agent_id,
            "execution_epoch": item.execution_epoch,
            "status": item.status,
            "lease_owner": item.lease_owner,
            "lease_expires_at": _iso(item.lease_expires_at),
            "heartbeat_at": _iso(item.heartbeat_at),
            "failure_code": item.failure_code,
            "error_code": item.error_code,
            "created_at": _iso(item.created_at),
            "started_at": _iso(item.started_at),
            "ended_at": _iso(item.ended_at),
        } for item in runners],
        "provider_attempts": [{
            "id": item.id,
            "runner_id": runner_id,
            "agent_turn_id": item.agent_turn_id,
            "provider": item.provider,
            "attempt_no": item.attempt_no,
            "status": item.status,
            "usage": dict(item.usage or {}),
            "error_code": item.error_code,
            "started_at": _iso(item.started_at),
            "ended_at": _iso(item.ended_at),
        } for item, runner_id in provider_attempts],
        "tool_invocations": [{
            "id": item.id,
            "runner_id": item.runner_id,
            "work_item_id": item.work_item_id,
            "tool_key": item.tool_key,
            "status": item.status,
            "attempt_count": item.attempt_count,
            "error_code": item.error_code,
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
        } for item in tool_invocations],
        "agent_invocations": [{
            "id": item.id,
            "caller_work_item_id": item.caller_work_item_id,
            "caller_agent_id": item.caller_agent_id,
            "target_work_item_id": item.target_work_item_id,
            "target_agent_id": item.target_agent_id,
            "status": item.status,
            "return_route": item.return_route,
            "request_hash": item.request_hash,
            "error_code": item.error_code,
            "created_at": _iso(item.created_at),
            "completed_at": _iso(item.completed_at),
        } for item in agent_invocations],
        "diagnostics": [group_diagnostic_view(item) for item in diagnostics],
        "timeline": [{
            "id": item.id,
            "sequence_no": item.sequence,
            "event_type": item.event_type,
            "visibility": item.visibility,
            "actor_type": item.actor_type,
            "actor_id": item.actor_id,
            "entity_type": item.entity_type,
            "entity_id": item.entity_id,
            "summary": item.summary,
            "caused_by_event_id": item.caused_by_event_id,
            "occurred_at": _iso(item.created_at),
        } for item in events],
        "outbox": [{
            "id": item.id,
            "topic": item.topic,
            "action": item.action,
            "aggregate_type": item.aggregate_type,
            "aggregate_id": item.aggregate_id,
            "status": item.status,
            "priority": item.priority,
            "attempt_count": item.attempt_count,
            "available_at": _iso(item.available_at),
            "lease_owner": item.dispatch_lease_owner,
            "lease_expires_at": _iso(item.dispatch_lease_expires_at),
            "has_error": bool(item.last_error),
            "created_at": _iso(item.created_at),
            "completed_at": _iso(item.completed_at),
        } for item in outbox],
    }
