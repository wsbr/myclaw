"""Best-effort, non-authoritative Runner observability persistence."""

from __future__ import annotations

import secrets

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.core.telemetry import emit_runtime_span
from app.models.common import utcnow, uuid7
from app.models.observability_v1 import ObservabilitySpan, ObservabilitySpanLink, RunnerTrace
from app.models.runtime_v1 import AgentInvocation, DurableRunner


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
        return


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
            trace.error_code = runner.error_code
            trace.ended_at = utcnow()
    except Exception:
        return
