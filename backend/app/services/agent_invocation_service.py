"""Authorization and durable dispatch for the invoke_agent Platform Tool."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_v1 import AgentBlueprint, AgentVersionBinding
from app.models.common import utcnow, uuid7
from app.models.group_v1 import GroupMembership, GroupRun, GroupRunMember
from app.models.runtime_v1 import AgentInvocation, DurableRunner, Run, ToolInvocation, WorkItem
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_new_work_runner
from app.services.agent_directory_service import resolve_callable_agent


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
    if target is None:
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
    group_run = await db.scalar(select(GroupRun).where(GroupRun.run_id == run.id)) if run.mode == "group" else None
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
    await ExecutionEventWriter(db).append(
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
    return invocation


async def dispatch_agent_invocation(
    db: AsyncSession, *, tool_invocation: ToolInvocation | None = None,
    operation_key: str | None = None, provider_call_id: str | None = None,
    caller_runner: DurableRunner, caller_work: WorkItem, run: Run, arguments: dict,
) -> AgentInvocation:
    target_agent_id = str(arguments.get("agent_id") or "").strip()
    request_text = str(arguments.get("task") or arguments.get("request") or "").strip()
    if not target_agent_id or not request_text:
        raise AgentInvocationRejected("agent_id and task are required")
    chain = list(caller_runner.principal_snapshot.get("delegation_chain") or [])
    if target_agent_id == caller_runner.agent_id:
        raise AgentInvocationRejected("an Agent cannot invoke itself")
    if run.mode in {"chat", "plan"} and chain:
        raise AgentInvocationRejected("sub Agents cannot invoke another Agent in Chat or Plan")
    if run.mode == "group" and (len(chain) >= 8 or target_agent_id in chain):
        raise AgentInvocationRejected("Group AgentInvocation cycle or maximum depth detected")
    target = await db.get(AgentBlueprint, target_agent_id)
    if target is None:
        raise AgentInvocationRejected("target Agent is unavailable")

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
                raise AgentInvocationRejected("caller is not a current Group member")
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
    existing = await db.scalar(select(AgentInvocation).where(
        AgentInvocation.caller_work_item_id == caller_work.id,
        AgentInvocation.idempotency_key == resolved_operation_key,
    ))
    if existing:
        expected_hash = _request_hash(
            target_agent_id=target_agent_id,
            request_text=request_text,
            input_data=dict(arguments.get("input") or {}),
        )
        if existing.request_hash != expected_hash:
            raise AgentInvocationRejected("AgentInvocation business idempotency collision")
        return existing

    invocation_id, target_work_id, target_runner_id = uuid7(), uuid7(), uuid7()
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
                        "group_membership_id": target_group_scope_id if run.mode == "group" else None},
    )
    agent_invocation = AgentInvocation(
        id=invocation_id, run_id=run.id,
        caller_work_item_id=caller_work.id, caller_runner_id=caller_runner.id,
        caller_agent_id=caller_runner.agent_id,
        target_work_item_id=target_work.id, target_agent_id=target_agent_id,
        target_agent_version_id=target_version_id, mode=run.mode,
        return_route="caller", request_text=request_text,
        context_snapshot={"tool_invocation_id": tool_invocation.id if tool_invocation else None,
                          "provider_call_id": provider_call_id,
                          "input": arguments.get("input") or {},
                          "group_session_id": group_run.group_session_id if run.mode == "group" else None,
                          "group_run_id": group_run.id if run.mode == "group" else None,
                          "source_group_membership_id": caller_group_scope_id if run.mode == "group" else None,
                          "target_group_membership_id": target_group_scope_id if run.mode == "group" else None},
        status="queued", batch_key=caller_runner.id,
        idempotency_key=resolved_operation_key, operation_key=resolved_operation_key,
        request_hash=_request_hash(
            target_agent_id=target_agent_id,
            request_text=request_text,
            input_data=dict(arguments.get("input") or {}),
        ),
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
    await db.flush()
    events = ExecutionEventWriter(db)
    created = await events.append(
        session_id=run.session_id, run_id=run.id, event_type="agent_invocation.created",
        event_dedupe_key=f"transition:agent_invocation:{agent_invocation.id}:0:1:create",
        actor_type="agent", actor_id=caller_runner.agent_id, entity_type="agent_invocation",
        entity_id=agent_invocation.id, entity_version=agent_invocation.version, visibility="user",
        summary="Agent collaboration request created",
        payload={"target_agent_id": target_agent_id},
        correlation_id=run.id,
    )
    queued = await events.append(
        session_id=run.session_id, run_id=run.id, event_type="runner.queued",
        event_dedupe_key=f"transition:runner:{target_runner.id}:0:1:agent_invocation",
        actor_type="service", actor_id="agent-invocation-gateway", entity_type="runner",
        entity_id=target_runner.id, entity_version=target_runner.version, visibility="private",
        private_owner=("runner", target_runner.id), summary="Invoked Agent Runner queued",
        payload={"agent_invocation_id": agent_invocation.id}, correlation_id=run.id,
        caused_by_event_id=created.id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.runner", action="execute", aggregate_type="runner", aggregate_id=target_runner.id,
        partition_key=run.session_id, dedupe_key=f"event:{queued.id}:execute",
        payload_ref={"runner_id": target_runner.id, "agent_invocation_id": agent_invocation.id},
    )
    return agent_invocation
