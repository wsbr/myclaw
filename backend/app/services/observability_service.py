"""Best-effort, non-authoritative Runner observability persistence."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets

from sqlalchemy import or_, select

from app.database import AsyncSessionLocal
from app.core.telemetry import emit_runtime_span
from app.models.common import utcnow, uuid7
from app.models.observability_v1 import ObservabilitySpan, ObservabilitySpanLink, RunnerTrace
from app.models.runtime_v1 import (
    AgentInvocation,
    AgentTurn,
    Approval,
    DurableRunner,
    Message,
    PlanUpdate,
    ProviderAttempt,
    ToolAttempt,
    ToolInvocation,
)
from app.models.verification_v1 import VerificationResult


logger = logging.getLogger(__name__)


async def ensure_runner_trace(runner_id: str, session_factory=AsyncSessionLocal) -> RunnerTrace | None:
    try:
        async with session_factory.begin() as db:
            existing = await db.scalar(select(RunnerTrace).where(RunnerTrace.runner_id == runner_id))
            if existing is not None:
                return existing
            runner = await db.get(DurableRunner, runner_id)
            if runner is None:
                return None
            trace = RunnerTrace(id=uuid7(), trace_id=secrets.token_hex(16), root_span_id=secrets.token_hex(8), runner_id=runner.id,
                session_id=runner.session_id, run_id=runner.run_id, work_item_id=runner.work_item_id,
                agent_id=runner.agent_id, status="running", sampled=True, started_at=utcnow())
            db.add(trace); await db.flush()
            invocation = await db.scalar(select(AgentInvocation).where(
                AgentInvocation.target_work_item_id == runner.work_item_id,
            ))
            if invocation is not None:
                caller_trace = await db.scalar(select(RunnerTrace).where(
                    RunnerTrace.runner_id == invocation.caller_runner_id,
                ))
                if caller_trace is not None:
                    now = utcnow()
                    _, span_id = emit_runtime_span(
                        trace_id=trace.trace_id, parent_span_id=trace.root_span_id,
                        name="invoke_agent.link", started_at=now, ended_at=now, status="ok",
                        attributes={"myclaw.agent_invocation.id": invocation.id},
                        linked_contexts=[(caller_trace.trace_id, caller_trace.root_span_id, {
                            "myclaw.link.type": "agent_invocation",
                            "myclaw.agent_invocation.id": invocation.id,
                        })],
                    )
                    span = ObservabilitySpan(id=uuid7(), runner_trace_id=trace.id,
                        span_id=span_id, parent_span_id=trace.root_span_id,
                        name="invoke_agent.link", span_kind="agent_invocation",
                        status="ok", started_at=now, ended_at=now, duration_ms=0,
                        attributes={"agent_invocation_id": invocation.id})
                    db.add(span); await db.flush()
                    db.add(ObservabilitySpanLink(id=uuid7(), span_id=span.id,
                        linked_trace_id=caller_trace.trace_id, linked_span_id=caller_trace.root_span_id,
                        link_type="agent_invocation", entity_type="agent_invocation", entity_id=invocation.id))
            return trace
    except Exception:
        logger.warning(
            "Failed to create durable Runner trace",
            exc_info=True,
            extra={"runner_id": runner_id},
        )
        return None


async def record_runner_span(
    runner_id: str, *, name: str, span_kind: str, started_at, ended_at,
    status: str = "ok", attributes: dict | None = None,
    input_tokens: int | None = None, output_tokens: int | None = None,
    error_code: str | None = None,
    session_factory=AsyncSessionLocal,
) -> None:
    try:
        trace = await ensure_runner_trace(runner_id, session_factory=session_factory)
        if trace is None:
            return
        duration_ms = max(0, int((ended_at - started_at).total_seconds() * 1000))
        async with session_factory.begin() as db:
            trace = await db.scalar(select(RunnerTrace).where(RunnerTrace.runner_id == runner_id))
            if trace is None: return
            safe_attributes = {
                "myclaw.runner.id": runner_id,
                "myclaw.run.id": trace.run_id,
                "myclaw.work_item.id": trace.work_item_id,
                "myclaw.agent.id": trace.agent_id,
                "myclaw.span.kind": span_kind,
                **{f"myclaw.{key}": value for key, value in dict(attributes or {}).items()},
            }
            _, span_id = emit_runtime_span(
                trace_id=trace.trace_id, parent_span_id=trace.root_span_id,
                name=name, started_at=started_at, ended_at=ended_at, status=status,
                attributes=safe_attributes,
            )
            db.add(ObservabilitySpan(id=uuid7(), runner_trace_id=trace.id,
                span_id=span_id, parent_span_id=trace.root_span_id, name=name, span_kind=span_kind,
                status=status, started_at=started_at, ended_at=ended_at,
                duration_ms=duration_ms, attributes=dict(attributes or {}),
                input_tokens=input_tokens, output_tokens=output_tokens, error_code=error_code))
    except Exception:
        logger.warning(
            "Failed to record durable Runner span",
            exc_info=True,
            extra={"runner_id": runner_id, "span_name": name},
        )
        return


def _fact_window(items, *, start_attr: str, end_attr: str):
    now = utcnow()
    starts = [getattr(item, start_attr, None) for item in items]
    ends = [getattr(item, end_attr, None) for item in items]
    starts = [item for item in starts if item is not None]
    ends = [item for item in ends if item is not None]
    started_at = min(starts) if starts else now
    ended_at = max(ends) if ends else started_at
    return started_at, max(started_at, ended_at)


def _fact_fingerprint(items) -> str:
    projection = [{
        "id": str(item.id),
        "status": str(getattr(item, "status", None) or "recorded"),
        "error_code": getattr(item, "error_code", None),
        "ended_at": str(getattr(item, "ended_at", None) or ""),
        "updated_at": str(getattr(item, "updated_at", None) or ""),
        "decided_at": str(getattr(item, "decided_at", None) or ""),
    } for item in items]
    return hashlib.sha256(json.dumps(
        projection, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


async def record_runner_fact_spans(
    runner_id: str, session_factory=AsyncSessionLocal,
) -> None:
    """Project committed Runner ledgers into bounded, credential-free spans.

    This runs after settlement, so it never changes execution truth and can see
    durable Tool, collaboration, WorkingPlan, and Verification facts created by
    the shared Agent loop.  One aggregate span per fact class keeps the Trace
    useful without copying prompts, arguments, messages, or artifact contents.
    """
    try:
        async with session_factory() as db:
            trace = await db.scalar(select(RunnerTrace).where(
                RunnerTrace.runner_id == runner_id,
            ))
            if trace is None:
                return
            existing_spans = list(await db.scalars(
                select(ObservabilitySpan).where(
                    ObservabilitySpan.runner_trace_id == trace.id,
                )
            ))
            existing_fact_fingerprints = {
                (span.name, (span.attributes or {}).get("fact_fingerprint"))
                for span in existing_spans
                if (span.attributes or {}).get("fact_fingerprint")
            }
            attempts = list(await db.scalars(
                select(ProviderAttempt).join(
                    AgentTurn, AgentTurn.id == ProviderAttempt.agent_turn_id,
                ).where(AgentTurn.runner_id == runner_id)
                .order_by(ProviderAttempt.started_at, ProviderAttempt.id)
            ))
            tools = list(await db.scalars(
                select(ToolInvocation).where(
                    ToolInvocation.runner_id == runner_id,
                ).order_by(ToolInvocation.created_at, ToolInvocation.id)
            ))
            tool_attempts = list(await db.scalars(
                select(ToolAttempt).join(
                    ToolInvocation,
                    ToolInvocation.id == ToolAttempt.tool_invocation_id,
                ).where(ToolInvocation.runner_id == runner_id)
                .order_by(ToolAttempt.created_at, ToolAttempt.id)
            ))
            turns = list(await db.scalars(
                select(AgentTurn).where(
                    AgentTurn.runner_id == runner_id,
                ).order_by(AgentTurn.turn_no, AgentTurn.id)
            ))
            invocations = list(await db.scalars(
                select(AgentInvocation).where(or_(
                    AgentInvocation.caller_runner_id == runner_id,
                    AgentInvocation.target_work_item_id == trace.work_item_id,
                )).order_by(AgentInvocation.created_at, AgentInvocation.id)
            ))
            plan_updates = list(await db.scalars(
                select(PlanUpdate).where(
                    PlanUpdate.updated_by_runner_id == runner_id,
                ).order_by(PlanUpdate.created_at, PlanUpdate.id)
            ))
            approvals = list(await db.scalars(
                select(Approval).where(
                    Approval.work_item_id == trace.work_item_id,
                ).order_by(Approval.created_at, Approval.id)
            ))
            group_messages = list(await db.scalars(
                select(Message).where(
                    Message.work_item_id == trace.work_item_id,
                    Message.scope_type == "group",
                ).order_by(Message.created_at, Message.id)
            ))
            verifications = list(await db.scalars(
                select(VerificationResult).where(
                    VerificationResult.run_id == trace.run_id,
                ).order_by(VerificationResult.created_at, VerificationResult.id)
            ))

        fact_groups = (
            ("agent.turn", "agent_turn", turns, "started_at", "ended_at"),
            ("provider.attempt", "provider", attempts, "started_at", "ended_at"),
            ("model_generation", "provider", attempts, "started_at", "ended_at"),
            ("tool.invocation", "tool", tools, "created_at", "updated_at"),
            ("tool.attempt", "tool_attempt", tool_attempts, "created_at", "updated_at"),
            ("agent.invocation", "agent_invocation", invocations, "created_at", "updated_at"),
            ("approval.wait", "approval", approvals, "created_at", "decided_at"),
            ("plan.update", "plan_update", plan_updates, "created_at", "created_at"),
            ("group.message", "group_message", group_messages, "created_at", "updated_at"),
            ("verification", "verification", verifications, "created_at", "created_at"),
        )
        for name, kind, items, start_attr, end_attr in fact_groups:
            if not items:
                continue
            started_at, ended_at = _fact_window(
                items, start_attr=start_attr, end_attr=end_attr,
            )
            statuses = sorted({
                str(getattr(item, "status", None) or "recorded") for item in items
            })
            error_items = [
                item for item in items
                if getattr(item, "status", None) in {
                    "failed", "timed_out", "outcome_unknown", "rejected",
                }
            ]
            input_tokens = output_tokens = None
            if name in {"agent.turn", "provider.attempt", "model_generation"}:
                input_tokens = 0
                output_tokens = 0
                for item in items:
                    usage = item.usage
                    if isinstance(usage, str):
                        usage = json.loads(usage)
                    usage = usage or {}
                    input_tokens += int(usage.get("input_tokens") or 0)
                    output_tokens += int(usage.get("output_tokens") or 0)
            attributes = {
                "fact_count": len(items),
                "fact_fingerprint": _fact_fingerprint(items),
                "statuses": statuses,
            }
            if name == "tool.invocation":
                attributes["tool_keys"] = sorted({item.tool_key for item in items})
            elif name == "approval.wait":
                attributes["approval_kinds"] = sorted({item.kind for item in items})
            elif name in {"provider.attempt", "model_generation"}:
                attributes["providers"] = sorted({item.provider for item in items})
            if (name, attributes["fact_fingerprint"]) in existing_fact_fingerprints:
                continue
            await record_runner_span(
                runner_id,
                name=name,
                span_kind=kind,
                started_at=started_at,
                ended_at=ended_at,
                status="error" if error_items else "ok",
                attributes=attributes,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error_code=(
                    str(getattr(error_items[0], "error_code", None) or statuses[0])
                    if error_items else None
                ),
                session_factory=session_factory,
            )
            existing_fact_fingerprints.add(
                (name, attributes["fact_fingerprint"])
            )
    except Exception:
        logger.warning(
            "Failed to project Runner ledger spans",
            exc_info=True,
            extra={"runner_id": runner_id},
        )


async def finish_runner_trace(runner_id: str, session_factory=AsyncSessionLocal) -> None:
    try:
        async with session_factory.begin() as db:
            trace = await db.scalar(select(RunnerTrace).where(
                RunnerTrace.runner_id == runner_id, RunnerTrace.status == "running",
            ).with_for_update())
            runner = await db.get(DurableRunner, runner_id)
            if trace is None or runner is None or runner.status == "running":
                return
            trace.status = ("completed" if runner.status == "succeeded" else
                            "cancelled" if runner.status == "cancelled" else "failed")
            trace.error_code = runner.error_code or runner.failure_code
            trace.ended_at = utcnow()
    except Exception:
        logger.warning(
            "Failed to finish durable Runner trace",
            exc_info=True,
            extra={"runner_id": runner_id},
        )
        return
