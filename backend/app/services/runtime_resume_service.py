"""Shared durable continuation construction for user and Tool wakeups."""

from __future__ import annotations

import json

from sqlalchemy import select

from app.core.tool_registry.base import ToolResult
from app.models.common import uuid7
from app.models.runtime_v1 import (
    AgentInvocation, Approval, CapabilitySnapshot, Clarification, DurableRunner, RunnerCheckpoint,
    ToolInvocation, WorkItem, Run,
)
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_replacement_runner
from app.runtime.tool_presentation import project_search_result, resolve_tool_presentation


NON_TERMINAL_TOOL = {
    "created", "waiting_confirmation", "waiting_resource", "queued", "running",
    "cancel_requested",
}


def merge_compaction_caches(checkpoint_entries, latest_entries) -> list[dict]:
    """Retain paid projections newer than a safe checkpoint, newest last.

    Recovery may rewind pending results. The compactor must revalidate every
    candidate against those canonical results before using it.
    """
    entries = {}
    for entry in [*(checkpoint_entries or []), *(latest_entries or [])]:
        if not isinstance(entry, dict):
            continue
        key = entry.get("id") or entry.get("source_hash")
        if not key:
            continue
        entries.pop(key, None)
        entries[key] = dict(entry)
    return list(entries.values())


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
    # Runtime coordination may finish admitting an operation while its domain
    # action still waits for a human/Agent result. Its acknowledgement is an
    # audit fact, not the provider's final Tool response. Those responses are
    # reconstructed by clarification_continuation / AgentInvocation settlement.
    invocations = [item for item in invocations if not (
        item.authorization_snapshot or {}
    ).get("runtime_durable_wait")]
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
                    "outcome_unknown" if invocation.status == "outcome_unknown" else "provider"
                ),
            ).model_dump(mode="json")
        elif invocation.tool_key == "search_tools" and result.get("status") == "succeeded":
            # Keep discovery's internal frozen definitions out of provider
            # continuation, just as the synchronous Agent loop does.
            capability_id = getattr(invocation, "capability_snapshot_id", None)
            capability = await db.get(CapabilitySnapshot, capability_id) if capability_id else None
            if capability is not None:
                principal = capability.execution_principal or {}
                presentation = await resolve_tool_presentation(
                    db, capability_snapshot=capability,
                    authentication_source=principal.get("authentication_source") or "web_session",
                )
                result = project_search_result(ToolResult.model_validate(result), presentation).model_dump(mode="json")
            else:
                result = {**result, "data": {
                    "tools": (result.get("data") or {}).get("tools", []),
                    "loaded_tool_definitions": [],
                }}
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
            "answered_by_account_id": clarification.target_account_id,
            "answered_by": (
                clarification.target_display_name_snapshot
                or clarification.target_handle_snapshot
                or clarification.target_user_id
            ),
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
    pending_agent = await db.scalar(select(AgentInvocation.id).where(
        AgentInvocation.caller_work_item_id == work_item_id,
        AgentInvocation.status.in_(("created", "queued", "running")),
    ).limit(1))
    return bool(
        pending_tool or pending_approval or pending_clarification or pending_agent
    )


def merge_pending_results(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Complete the last unfinished batch without duplicating its calls.

    Durable wakeups reconstruct the waiting assistant/tool batch from database
    facts. Keep its original assistant text and arguments, plus any synchronous
    results already checkpointed. Only the unfinished trailing batch may merge:
    a provider may legitimately reuse a call ID in a later, completed turn.
    """

    merged = [dict(item) for item in existing]
    incoming = [dict(item) for item in incoming]
    if not incoming or incoming[0].get("role") != "assistant" or not incoming[0].get("tool_calls"):
        return [*merged, *incoming]
    batch_index = next((index for index in range(len(merged) - 1, -1, -1)
                        if merged[index].get("role") == "assistant" and merged[index].get("tool_calls")), None)
    if batch_index is None:
        return [*merged, *incoming]
    original_calls = list(merged[batch_index]["tool_calls"])
    call_ids = {str(call["id"]) for call in original_calls if call.get("id")}
    batch_end = batch_index + 1
    completed = set()
    while batch_end < len(merged) and merged[batch_end].get("role") == "tool":
        message = merged[batch_end]
        completed.add(str(message.get("tool_call_id") or message.get("tool_use_id") or ""))
        batch_end += 1
    incoming_calls = incoming[0]["tool_calls"]
    incoming_ids = {str(call["id"]) for call in incoming_calls if call.get("id")}
    if not (call_ids - completed) or not call_ids.intersection(incoming_ids):
        return [*merged, *incoming]
    added_calls = [call for call in incoming_calls if str(call.get("id")) not in call_ids]
    if added_calls:
        merged[batch_index] = {
            **merged[batch_index], "tool_calls": [*original_calls, *added_calls],
        }
    call_ids.update(incoming_ids)
    for message in incoming[1:]:
        call_id = str(message.get("tool_call_id") or message.get("tool_use_id") or "")
        if message.get("role") == "tool" and call_id in call_ids:
            if call_id not in completed:
                merged.insert(batch_end, message)
                batch_end += 1
                completed.add(call_id)
        else:
            merged.append(message)
    return merged


async def queue_work_item_resume(
    db, *, work: WorkItem, run: Run, source_runner: DurableRunner,
    pending_results: list[dict], reason: str, caused_by_event_id: str,
    actor_id: str, principal_snapshot_updates: dict | None = None,
    replace_system_notice_prefix: str | None = None,
) -> DurableRunner:
    if work.active_runner_id is not None:
        raise RuntimeError("WorkItem already has an active Runner")
    # A durable Tool/Agent wait replaces the Runner, but it must not replace
    # the conversation state accumulated by earlier waits on the same
    # WorkItem. AgentTurn rows are immutable execution evidence rather than
    # chat history, so the replacement Runner is the only continuation carrier
    # for those authoritative assistant/tool pairs. Keeping only the newest
    # batch makes a model see the original goal plus one result and can cause it
    # to repeat already-committed side effects.
    prior_results = list(
        (source_runner.input_snapshot or {}).get("pending_results") or []
    )
    latest_checkpoint = await db.scalar(select(RunnerCheckpoint).where(
        RunnerCheckpoint.runner_id == source_runner.id,
        RunnerCheckpoint.safe_to_resume.is_(True),
    ).order_by(RunnerCheckpoint.sequence_no.desc()).limit(1))
    current_runner_results = list(
        (latest_checkpoint.state_snapshot or {}).get("pending_results") or []
    ) if latest_checkpoint is not None else []
    cumulative_results = merge_pending_results(prior_results, current_runner_results)
    if replace_system_notice_prefix:
        cumulative_results = [
            item for item in cumulative_results
            if not (
                item.get("role") == "system"
                and str(item.get("content") or "").startswith(
                    replace_system_notice_prefix
                )
            )
        ]
    cumulative_results = merge_pending_results(cumulative_results, pending_results)
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
        id=uuid7(), tenant_id=run.tenant_id,
        work_item_id=work.id, run_id=run.id,
        session_id=run.session_id, agent_id=source_runner.agent_id,
        agent_version_id=source_runner.agent_version_id,
        execution_epoch=work.execution_epoch, fencing_token=work.execution_epoch,
        status="queued", principal_snapshot={
            **dict(source_runner.principal_snapshot or {}),
            **dict(principal_snapshot_updates or {}),
        },
        assembly_snapshot=dict(source_runner.assembly_snapshot or {}), prompt_manifest={},
        input_snapshot={
            **dict(source_runner.input_snapshot or {}),
            "resume_reason": reason, "pending_results": cumulative_results,
            "hard_compactions": merge_compaction_caches(
                (latest_checkpoint.state_snapshot or {}).get("hard_compactions", [])
                if latest_checkpoint is not None else [],
                (source_runner.input_snapshot or {}).get("hard_compactions", []),
            ),
            "loaded_tool_version_ids": list(
                (latest_checkpoint.loaded_tool_version_ids if latest_checkpoint else None)
                or (source_runner.input_snapshot or {}).get("loaded_tool_version_ids")
                or []
            ),
        },
    )
    run.status = "active"
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
        tenant_id=run.tenant_id, account_id=run.account_id,
        payload_ref={"runner_id": replacement.id, "reason": reason},
    )
    return replacement
