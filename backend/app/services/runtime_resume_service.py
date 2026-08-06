"""Shared durable continuation construction for user and Tool wakeups."""

from __future__ import annotations

import json

from sqlalchemy import select

from app.core.tool_registry.base import ToolResult
from app.models.common import uuid7
from app.models.runtime_v1 import (
    Approval, Clarification, DurableRunner, ToolInvocation, WorkItem, Run,
)
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_replacement_runner


NON_TERMINAL_TOOL = {
    "created", "waiting_confirmation", "waiting_resource", "queued", "running",
    "cancel_requested",
}


def _tool_message(call_id: str, result: dict) -> dict:
    return {
        "role": "tool", "tool_use_id": call_id, "tool_call_id": call_id,
        "content": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
    }


async def tool_continuation(
    db, *, work_item_id: str, source_turn_id: str,
) -> list[dict]:
    invocations = list(await db.scalars(select(ToolInvocation).where(
        ToolInvocation.work_item_id == work_item_id,
        ToolInvocation.source_turn_id == source_turn_id,
    ).order_by(ToolInvocation.created_at, ToolInvocation.id)))
    if not invocations:
        return []
    assistant = {
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": item.provider_tool_call_id,
            "type": "tool_use", "name": item.tool_key,
            "input": item.arguments,
        } for item in invocations],
    }
    messages = [assistant]
    for invocation in invocations:
        result = invocation.result_data
        if result is None:
            error = invocation.error_detail or invocation.error_code or "Tool did not complete"
            result = ToolResult(
                status="failed", summary=error,
                error_code=invocation.error_code or "tool_failed",
                error=error,
                error_category=(
                    "outcome_unknown" if invocation.status == "outcome_unknown" else "permanent"
                ),
            ).model_dump(mode="json")
        messages.append(_tool_message(invocation.provider_tool_call_id, result))
    return messages


def clarification_continuation(clarification: Clarification) -> list[dict]:
    call_id = clarification.provider_tool_call_id or f"clarification-{clarification.id}"
    return [{
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": call_id, "type": "tool_use", "name": "ask_user",
            "input": {"question": clarification.question},
        }],
    }, _tool_message(call_id, ToolResult(
        status="succeeded", summary="User answered the clarification",
        data={
            "clarification_id": clarification.id,
            "answer": clarification.answer_text or "",
        },
    ).model_dump(mode="json"))]


async def has_pending_actions(db, *, work_item_id: str) -> bool:
    pending_tool = await db.scalar(select(ToolInvocation.id).where(
        ToolInvocation.work_item_id == work_item_id,
        ToolInvocation.status.in_(NON_TERMINAL_TOOL),
    ).limit(1))
    pending_approval = await db.scalar(select(Approval.id).where(
        Approval.work_item_id == work_item_id, Approval.status == "pending",
    ).limit(1))
    pending_clarification = await db.scalar(select(Clarification.id).where(
        Clarification.work_item_id == work_item_id, Clarification.status == "open",
    ).limit(1))
    return bool(pending_tool or pending_approval or pending_clarification)


async def queue_work_item_resume(
    db, *, work: WorkItem, run: Run, source_runner: DurableRunner,
    pending_results: list[dict], reason: str, caused_by_event_id: str,
    actor_id: str,
) -> DurableRunner:
    if work.active_runner_id is not None:
        raise RuntimeError("WorkItem already has an active Runner")
    work.execution_epoch += 1
    work.attempt_no += 1
    work.status = "queued"
    work.wait_reason = None
    work.resume_cursor = {
        **dict(work.resume_cursor or {}),
        "reason": reason, "caused_by_event_id": caused_by_event_id,
    }
    work.version += 1
    replacement = DurableRunner(
        id=uuid7(), work_item_id=work.id, run_id=run.id,
        session_id=run.session_id, agent_id=source_runner.agent_id,
        agent_version_id=source_runner.agent_version_id,
        execution_epoch=work.execution_epoch, fencing_token=work.execution_epoch,
        status="queued", principal_snapshot=dict(source_runner.principal_snapshot or {}),
        assembly_snapshot=dict(source_runner.assembly_snapshot or {}), prompt_manifest={},
        input_snapshot={
            **dict(source_runner.input_snapshot or {}),
            "resume_reason": reason, "pending_results": pending_results,
        },
    )
    run.status = "active"
    run.lifecycle = "running"
    run.wait_reason = None
    run.version += 1
    await persist_replacement_runner(db, work, replacement)
    resumed = await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id,
        event_type="run.resumed",
        event_dedupe_key=f"run:{run.id}:resumed:epoch:{work.execution_epoch}",
        actor_type="service", actor_id=actor_id,
        entity_type="run", entity_id=run.id, entity_version=run.version,
        visibility="user", summary=f"Run resumed after {reason}",
        payload={"work_item_id": work.id, "reason": reason},
        correlation_id=run.id, caused_by_event_id=caused_by_event_id,
    )
    queued = await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id, event_type="runner.queued",
        event_dedupe_key=f"runner:{replacement.id}:queued:{reason}",
        actor_type="service", actor_id=actor_id,
        entity_type="runner", entity_id=replacement.id,
        entity_version=replacement.version, visibility="private",
        private_owner=("runner", replacement.id),
        summary=f"Runner queued after {reason}", payload={},
        correlation_id=run.id, caused_by_event_id=resumed.id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.runner", action="execute", aggregate_type="runner",
        aggregate_id=replacement.id, partition_key=run.session_id,
        dedupe_key=f"event:{queued.id}:execute",
        payload_ref={"runner_id": replacement.id, "reason": reason},
    )
    return replacement
