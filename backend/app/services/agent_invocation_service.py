"""Authorization and durable dispatch for the invoke_agent Platform Tool."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_v1 import AgentBlueprint, AgentVersionBinding
from app.models.common import utcnow, uuid7
from app.models.foundation import ExecutionEvent
from app.models.group_v1 import GroupMembership, GroupRun, GroupRunMember
from app.models.runtime_v1 import (
    AgentInvocation,
    DurableRunner,
    Message,
    Run,
    ToolInvocation,
    WorkItem,
)
from app.persistence.reliable_writes import EventRecipient, ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_new_work_runner
from app.services.agent_directory_service import resolve_callable_agent
from app.services.group_diagnostic_service import record_group_invocation_guard
from app.services.group_collaboration_service import _next_group_message_sequence


class AgentInvocationRejected(ValueError):
    def __init__(self, message: str, code: str = "agent_invocation_rejected"):
        super().__init__(message)
        self.code = code


TERMINAL_WORK = {"succeeded", "failed", "timed_out", "cancelled", "abandoned", "blocked"}


def _request_hash(*, target_agent_id: str, request_text: str, input_data: dict) -> str:
    return hashlib.sha256(json.dumps({
        "target_agent_id": target_agent_id,
        "request_text": request_text,
        "input": input_data,
        "return_route": "caller",
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def persist_rejected_agent_invocation(
    db: AsyncSession, *, caller_runner: DurableRunner, caller_work: WorkItem,
    run: Run, arguments: dict, operation_key: str, reason: str,
    reason_code: str = "agent_invocation_rejected",
) -> AgentInvocation | None:
    """Persist a denied call when the requested target is a real Agent.

    A random/nonexistent identifier cannot satisfy the target FK; that remains
    a schema/lookup error. Policy and visibility denials for real candidates
    are durable audit facts and never create a target WorkItem.
    """
    target_agent_id = str(arguments.get("agent_id") or "").strip()
    target = await db.get(AgentBlueprint, target_agent_id) if target_agent_id else None
    group_run = await db.scalar(select(GroupRun).where(
        GroupRun.run_id == run.id,
    ).with_for_update()) if run.mode == "group" else None
    if target is None:
        await record_group_invocation_guard(
            db,
            run=run,
            caller_runner=caller_runner,
            caller_work=caller_work,
            arguments=arguments,
            operation_key=operation_key,
            reason_code=reason_code,
            reason=reason,
        )
        return None
    existing = await db.scalar(select(AgentInvocation).where(
        AgentInvocation.caller_work_item_id == caller_work.id,
        AgentInvocation.idempotency_key == operation_key,
    ))
    if existing is not None:
        expected_hash = _request_hash(
            target_agent_id=target.id,
            request_text=str(arguments.get("task") or arguments.get("request") or "").strip(),
            input_data=dict(arguments.get("input") or {}),
        )
        if existing.request_hash != expected_hash:
            raise AgentInvocationRejected("AgentInvocation business idempotency collision")
        return existing
    invocation = AgentInvocation(
        id=uuid7(), run_id=run.id, caller_work_item_id=caller_work.id,
        caller_runner_id=caller_runner.id, caller_agent_id=caller_runner.agent_id,
        target_agent_id=target.id,
        target_agent_version_id=target.current_published_version_id,
        mode=run.mode, return_route="caller",
        request_text=str(arguments.get("task") or arguments.get("request") or "").strip(),
        context_snapshot={}, status="rejected", batch_key=caller_runner.id,
        result_summary=None, result_data=None,
        error_code=reason_code, error_detail=reason[:4000],
        idempotency_key=operation_key, operation_key=operation_key,
        request_hash=_request_hash(
            target_agent_id=target.id,
            request_text=str(arguments.get("task") or arguments.get("request") or "").strip(),
            input_data=dict(arguments.get("input") or {}),
        ),
        authorization_snapshot={
            "account_id": caller_runner.principal_snapshot.get("account_id"),
            "permission_mode": run.permission_mode,
            "permission_revision": caller_runner.principal_snapshot.get("permission_revision"),
        },
        completed_at=utcnow(),
        deadline_at=run.deadline_at,
        group_session_id=group_run.group_session_id if group_run else None,
        group_run_id=group_run.id if group_run else None,
    )
    db.add(invocation)
    await db.flush()
    rejected_event = await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id,
        event_type="agent_invocation.rejected",
        event_dedupe_key=f"agent_invocation:{invocation.id}:rejected",
        actor_type="agent", actor_id=caller_runner.agent_id,
        entity_type="agent_invocation", entity_id=invocation.id,
        entity_version=invocation.version, visibility="user",
        summary="Agent collaboration request rejected",
        payload={
            "target_agent_id": target.id,
            "reason_code": reason_code,
        },
        correlation_id=run.id,
    )
    await record_group_invocation_guard(
        db,
        run=run,
        caller_runner=caller_runner,
        caller_work=caller_work,
        arguments=arguments,
        operation_key=operation_key,
        reason_code=reason_code,
        reason=reason,
        invocation=invocation,
        source_event_id=rejected_event.id,
    )
    return invocation


async def dispatch_agent_invocation(
    db: AsyncSession, *, tool_invocation: ToolInvocation | None = None,
    operation_key: str | None = None, provider_call_id: str | None = None,
    caller_runner: DurableRunner, caller_work: WorkItem, run: Run, arguments: dict,
) -> AgentInvocation:
    target_agent_id = str(arguments.get("agent_id") or "").strip()
    request_text = str(arguments.get("task") or arguments.get("request") or "").strip()
    if not target_agent_id or not request_text:
        raise AgentInvocationRejected(
            "agent_id and task are required", "agent_invocation_arguments_invalid",
        )
    chain = list(caller_runner.principal_snapshot.get("delegation_chain") or [])
    if target_agent_id == caller_runner.agent_id:
        raise AgentInvocationRejected(
            "an Agent cannot invoke itself", "agent_invocation_self_cycle",
        )
    if run.mode in {"chat", "plan"} and chain:
        raise AgentInvocationRejected(
            "sub Agents cannot invoke another Agent in Chat or Plan",
            "agent_invocation_nested_direct_mode",
        )
    if run.mode == "chat":
        selected_group = await db.scalar(select(ExecutionEvent.id).where(
            ExecutionEvent.session_id == run.session_id,
            ExecutionEvent.event_dedupe_key == f"group-emergence-selection:{run.id}",
        ).limit(1))
        if selected_group is not None:
            raise AgentInvocationRejected(
                "This Chat already selected a post-Chat Group; dispatch work from the emerged Group instead",
                "agent_invocation_after_group_selection",
            )
    if run.mode == "group":
        max_depth = int(
            (run.hard_budget_snapshot or {}).get("max_agent_depth") or 8
        )
        if len(chain) >= max_depth:
            raise AgentInvocationRejected(
                "Group AgentInvocation maximum depth detected",
                "group_invocation_max_depth",
            )
        if target_agent_id in chain:
            raise AgentInvocationRejected(
                "Group AgentInvocation would re-enter its causal Agent chain",
                "group_invocation_cycle",
            )
    target = await db.get(AgentBlueprint, target_agent_id)
    if target is None:
        raise AgentInvocationRejected(
            "target Agent is unavailable", "agent_invocation_target_unavailable",
        )

    if run.mode == "plan":
        try:
            target, _card = await resolve_callable_agent(
                db,
                account_id=caller_runner.principal_snapshot.get("account_id"),
                actor_agent_id=caller_runner.agent_id,
                target_agent_id=target_agent_id,
            )
        except (LookupError, PermissionError) as error:
            raise AgentInvocationRejected(str(error)) from error
        target_version_id = target.current_published_version_id
    elif run.mode == "chat":
        # Keep configured bindings as a trusted-candidate path, but do not turn
        # them into a topology: a current visible AgentCard is equally valid.
        bound = await db.scalar(select(AgentVersionBinding).where(
            AgentVersionBinding.agent_version_id == caller_runner.agent_version_id,
            AgentVersionBinding.bound_agent_id == target_agent_id,
        ))
        if bound is None:
            try:
                target, _card = await resolve_callable_agent(
                    db,
                    account_id=caller_runner.principal_snapshot.get("account_id"),
                    actor_agent_id=caller_runner.agent_id,
                    target_agent_id=target_agent_id,
                )
            except (LookupError, PermissionError) as error:
                raise AgentInvocationRejected(str(error)) from error
        if target.status != "published" or not target.current_published_version_id:
            raise AgentInvocationRejected("target Agent has no active published version")
        target_version_id = target.current_published_version_id
    elif run.mode == "group":
        group_run = await db.scalar(select(GroupRun).where(GroupRun.run_id == run.id).with_for_update())
        if group_run is None:
            raise AgentInvocationRejected("GroupRun is unavailable")
        caller_scope_id = caller_runner.principal_snapshot.get("group_membership_id")
        native_target = await db.scalar(select(GroupRunMember).where(
            GroupRunMember.group_run_id == group_run.id,
            GroupRunMember.agent_id == target_agent_id,
        ))
        if native_target is not None:
            caller_membership = await db.scalar(select(GroupMembership).where(
                GroupMembership.id == caller_scope_id,
                GroupMembership.group_session_id == group_run.group_session_id,
                GroupMembership.agent_id == caller_runner.agent_id,
                GroupMembership.status == "joined",
            ))
            target_membership = await db.get(GroupMembership, native_target.membership_id)
            if caller_membership is None or target_membership is None:
                raise AgentInvocationRejected(
                    "caller is not a current Group member",
                    "group_caller_membership_invalid",
                )
            if native_target.participation_status != "available" or not native_target.agent_version_id:
                raise AgentInvocationRejected(
                    "target Group member is unavailable in this GroupRun",
                    "group_member_unavailable",
                )
            target_version_id = native_target.agent_version_id
            target_group_scope_id = target_membership.id
            caller_group_scope_id = caller_membership.id
        else:
            # A real but graph-external Agent is a durable rejected Invocation
            # under the first-phase fixed_for_run policy.
            raise AgentInvocationRejected(
                "target Agent is outside the fixed GroupRun membership snapshot",
                "membership_policy_fixed_for_run",
            )
    else:
        raise AgentInvocationRejected("unsupported AgentInvocation mode")

    resolved_operation_key = (
        tool_invocation.business_idempotency_key if tool_invocation is not None
        else str(operation_key or "").strip()
    )
    if not resolved_operation_key:
        raise AgentInvocationRejected("AgentInvocation operation key is required")
    request_hash = _request_hash(
        target_agent_id=target_agent_id,
        request_text=request_text,
        input_data=dict(arguments.get("input") or {}),
    )
    existing = await db.scalar(select(AgentInvocation).where(
        AgentInvocation.caller_work_item_id == caller_work.id,
        AgentInvocation.idempotency_key == resolved_operation_key,
    ))
    if existing:
        if existing.request_hash != request_hash:
            raise AgentInvocationRejected("AgentInvocation business idempotency collision")
        if existing.status == "rejected":
            raise AgentInvocationRejected(
                existing.error_detail or "AgentInvocation was rejected",
                existing.error_code or "agent_invocation_rejected",
            )
        return existing
    if run.mode == "group":
        duplicate = await db.scalar(select(AgentInvocation.id).where(
            AgentInvocation.run_id == run.id,
            AgentInvocation.caller_work_item_id == caller_work.id,
            AgentInvocation.caller_agent_id == caller_runner.agent_id,
            AgentInvocation.target_agent_id == target_agent_id,
            AgentInvocation.request_hash == request_hash,
            AgentInvocation.status != "rejected",
        ).limit(1))
        if duplicate is not None:
            raise AgentInvocationRejected(
                "The same Group task was already delegated from this WorkItem",
                "group_duplicate_delegation",
            )
        max_fan_out = int(
            (run.hard_budget_snapshot or {}).get(
                "max_agent_fan_out_per_work_item",
            ) or 8
        )
        prior_targets = set(await db.scalars(select(
            AgentInvocation.target_agent_id,
        ).where(
            AgentInvocation.run_id == run.id,
            AgentInvocation.caller_work_item_id == caller_work.id,
            AgentInvocation.status != "rejected",
        )))
        if target_agent_id not in prior_targets and len(prior_targets) >= max_fan_out:
            raise AgentInvocationRejected(
                "Group AgentInvocation fan-out limit reached for this WorkItem",
                "group_fan_out_exhausted",
            )

    invocation_id, target_work_id, target_runner_id = uuid7(), uuid7(), uuid7()
    request_message_id = uuid7() if run.mode == "group" else None
    target_work = WorkItem(
        id=target_work_id, run_id=run.id, session_id=run.session_id, user_id=run.user_id,
        mode=run.mode, owner_agent_id=target_agent_id, agent_version_id=target_version_id,
        parent_work_item_id=caller_work.id,
        group_run_id=group_run.id if run.mode == "group" else None,
        group_membership_id=target_group_scope_id if run.mode == "group" else None,
        goal=request_text, purpose="delegation", status="queued", execution_epoch=1, attempt_no=1,
    )
    target_runner = DurableRunner(
        id=target_runner_id, work_item_id=target_work.id, run_id=run.id, session_id=run.session_id,
        agent_id=target_agent_id, agent_version_id=target_version_id, execution_epoch=1, status="queued",
        principal_snapshot={**dict(caller_runner.principal_snapshot), "current_agent_id": target_agent_id,
                            "group_membership_id": target_group_scope_id if run.mode == "group" else caller_runner.principal_snapshot.get("group_membership_id"),
                            "delegation_chain": [*chain, caller_runner.agent_id]},
        assembly_snapshot={"agent_version_id": target_version_id}, prompt_manifest={},
        input_snapshot={"agent_invocation_id": invocation_id, "request_text": request_text,
                        "context": arguments.get("input") or {},
                        "group_session_id": group_run.group_session_id if run.mode == "group" else None,
                        "group_run_id": group_run.id if run.mode == "group" else None,
                        "group_membership_id": target_group_scope_id if run.mode == "group" else None,
                        "source_message_id": request_message_id},
    )
    handoff_kind = (
        "ownership_transfer"
        if run.mode == "group" and caller_work.id == run.root_work_item_id
        and caller_work.parent_work_item_id is None
        else "subtask"
    )
    agent_invocation = AgentInvocation(
        id=invocation_id, run_id=run.id,
        caller_work_item_id=caller_work.id, caller_runner_id=caller_runner.id,
        caller_agent_id=caller_runner.agent_id,
        target_work_item_id=target_work.id, target_agent_id=target_agent_id,
        target_agent_version_id=target_version_id, mode=run.mode,
        return_route="group" if run.mode == "group" else "caller",
        request_text=request_text,
        context_snapshot={"tool_invocation_id": tool_invocation.id if tool_invocation else None,
                          "provider_call_id": provider_call_id,
                          "input": arguments.get("input") or {},
                          "dispatch_mode": "async_handoff" if run.mode == "group" else "awaited",
                          "handoff_kind": handoff_kind,
                          "group_session_id": group_run.group_session_id if run.mode == "group" else None,
                          "group_run_id": group_run.id if run.mode == "group" else None,
                          "source_group_membership_id": caller_group_scope_id if run.mode == "group" else None,
                          "target_group_membership_id": target_group_scope_id if run.mode == "group" else None,
                          "request_message_id": request_message_id},
        status="queued", batch_key=caller_runner.id,
        idempotency_key=resolved_operation_key, operation_key=resolved_operation_key,
        request_hash=request_hash,
        authorization_snapshot={
            "account_id": caller_runner.principal_snapshot.get("account_id"),
            "permission_mode": run.permission_mode,
            "permission_revision": caller_runner.principal_snapshot.get("permission_revision"),
            "caller_agent_id": caller_runner.agent_id,
            "target_agent_version_id": target_version_id,
        },
        group_session_id=group_run.group_session_id if run.mode == "group" else None,
        group_run_id=group_run.id if run.mode == "group" else None,
        deadline_at=run.deadline_at,
    )
    if tool_invocation is not None:
        tool_invocation.status = "waiting_resource"
        tool_invocation.attempt_count += 1
        tool_invocation.version += 1
    await persist_new_work_runner(db, target_work, target_runner)
    db.add(agent_invocation)
    request_message = None
    if run.mode == "group":
        # AgentInvocation remains the scheduling authority. This Message is
        # its human-visible directed @ projection; it must not create another
        # Runner or a second delivery path.
        request_message = Message(
            id=request_message_id,
            session_id=run.session_id,
            user_id=run.user_id,
            scope_type="group",
            group_session_id=group_run.group_session_id,
            group_run_id=group_run.id,
            sender_type="agent",
            sender_agent_id=caller_runner.agent_id,
            recipient_type="agent",
            recipient_agent_id=target_agent_id,
            recipient_membership_id=target_group_scope_id,
            visibility="direct",
            role="assistant",
            content=request_text,
            content_format="markdown",
            status="committed",
            run_id=run.id,
            work_item_id=caller_work.id,
            operation_key=f"agent_invocation:{invocation_id}:request",
            sequence_no=await _next_group_message_sequence(
                db, group_run.group_session_id,
            ),
        )
        db.add(request_message)
    await db.flush()
    events = ExecutionEventWriter(db)
    created = await events.append(
        session_id=run.session_id, run_id=run.id, event_type="agent_invocation.created",
        event_dedupe_key=f"transition:agent_invocation:{agent_invocation.id}:0:1:create",
        actor_type="agent", actor_id=caller_runner.agent_id, entity_type="agent_invocation",
        entity_id=agent_invocation.id, entity_version=agent_invocation.version, visibility="user",
        summary="Agent collaboration request created",
        payload={
            "target_agent_id": target_agent_id,
            "dispatch_mode": (
                "async_handoff" if run.mode == "group" else "awaited"
            ),
            "handoff_kind": handoff_kind,
        },
        correlation_id=run.id,
    )
    request_event = None
    if request_message is not None:
        request_event = await events.append(
            session_id=run.session_id,
            run_id=run.id,
            group_session_id=group_run.group_session_id,
            group_run_id=group_run.id,
            event_type="group.message_created",
            event_dedupe_key=f"agent_invocation:{agent_invocation.id}:request-message",
            actor_type="agent",
            actor_id=caller_runner.agent_id,
            entity_type="message",
            entity_id=request_message.id,
            visibility="direct",
            recipients=(
                EventRecipient("agent", target_agent_id),
                EventRecipient("user_identity", run.user_id),
            ),
            summary="Agent delegated work with a directed @ message",
            payload={
                "message_id": request_message.id,
                "agent_invocation_id": agent_invocation.id,
                "sender_agent_id": caller_runner.agent_id,
                "recipient_agent_id": target_agent_id,
            },
            correlation_id=run.id,
            caused_by_event_id=created.id,
        )
        request_message.caused_by_event_id = request_event.id
    queued = await events.append(
        session_id=run.session_id, run_id=run.id, event_type="runner.queued",
        event_dedupe_key=f"transition:runner:{target_runner.id}:0:1:agent_invocation",
        actor_type="service", actor_id="agent-invocation-gateway", entity_type="runner",
        entity_id=target_runner.id, entity_version=target_runner.version, visibility="private",
        private_owner=("runner", target_runner.id), summary="Invoked Agent Runner queued",
        payload={"agent_invocation_id": agent_invocation.id}, correlation_id=run.id,
        caused_by_event_id=request_event.id if request_event else created.id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.runner", action="execute", aggregate_type="runner", aggregate_id=target_runner.id,
        partition_key=run.session_id, dedupe_key=f"event:{queued.id}:execute",
        payload_ref={"runner_id": target_runner.id, "agent_invocation_id": agent_invocation.id},
    )
    return agent_invocation
