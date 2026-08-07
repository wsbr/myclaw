"""Agent-native Group creation, messaging, snapshots, and projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import func, select

from app.models.agent_v1 import AgentAssemblyVersion, AgentCard
from app.models.common import utcnow, uuid7
from app.models.foundation import ExecutionEvent, UserSession
from app.models.group_v1 import (
    GroupMembership,
    GroupRun,
    GroupRunMember,
    GroupSession,
    GroupSummaryRequest,
    SharedTask,
)
from app.models.runtime_v1 import DurableRunner, Message, Run, WorkItem
from app.persistence.reliable_writes import (
    EventRecipient,
    ExecutionEventWriter,
    OutboxWriter,
)
from app.persistence.runtime_graph import persist_new_work_runner
from app.services.agent_directory_service import resolve_callable_agent
from app.services.run_reliability_service import default_run_budget, deadline_from_budget


class GroupPolicyError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class ResolvedGroupMember:
    agent_id: str
    version: AgentAssemblyVersion
    card: AgentCard


async def _resolve_group_members(
    db, *, account_id: str, agent_ids: Iterable[str], actor_agent_id: str,
) -> list[ResolvedGroupMember]:
    normalized = [str(item).strip() for item in agent_ids if str(item).strip()]
    if len(normalized) != 3 or len(set(normalized)) != len(normalized):
        raise GroupPolicyError(
            "invalid_group_members", "Phase-one Group requires exactly 3 unique Agent IDs"
        )
    resolved: list[ResolvedGroupMember] = []
    for agent_id in normalized:
        try:
            agent, card = await resolve_callable_agent(
                db,
                account_id=account_id,
                actor_agent_id=actor_agent_id,
                target_agent_id=agent_id,
            )
        except PermissionError as error:
            raise GroupPolicyError("agent_not_discoverable", str(error)) from error
        except LookupError as error:
            raise GroupPolicyError("agent_unavailable", str(error)) from error
        if not card.group_enabled:
            raise GroupPolicyError(
                "agent_group_disabled", f"Agent {agent_id} is not enabled for Group"
            )
        governance = dict(card.collaboration_summary or {}).get("governance") or {}
        if isinstance(governance, dict) and governance.get("group_allowed") is False:
            raise GroupPolicyError(
                "group_governance_denied",
                f"Agent {agent_id} is denied by Group governance",
            )
        version = await db.get(AgentAssemblyVersion, card.agent_version_id)
        if (
            version is None
            or version.publication_status != "published"
            or version.immutable_at is None
        ):
            raise GroupPolicyError(
                "agent_unavailable", f"Agent {agent_id} has no usable published version"
            )
        resolved.append(ResolvedGroupMember(agent.id, version, card))
    return resolved


def group_session_view(group: GroupSession) -> dict:
    return {
        "id": group.id,
        "session_id": group.session_id,
        "status": group.status,
        "initiator_agent_id": group.initiator_agent_id,
        "membership_policy": group.membership_policy,
        "version": group.version,
    }


def membership_view(row: GroupMembership) -> dict:
    return {
        "id": row.id,
        "group_session_id": row.group_session_id,
        "agent_id": row.agent_id,
        "status": row.status,
        "invited_by_kind": row.invited_by_kind,
        "reason": row.reason,
        "context_scope": dict(row.context_scope or {}),
        "authorization_source": row.authorization_source,
        "joined_at": row.joined_at.isoformat() if row.joined_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "version": row.version,
    }


def group_run_member_view(row: GroupRunMember) -> dict:
    return {
        "agent_id": row.agent_id,
        "membership_id": row.membership_id,
        "agent_version_id": row.agent_version_id,
        "participation_status": row.participation_status,
        "unavailable_reason": row.unavailable_reason,
        "membership_policy": row.membership_policy,
        "context_scope": dict(row.context_scope or {}),
        "capability_boundary": dict(row.capability_boundary or {}),
        "budget_snapshot": dict(row.budget_snapshot or {}),
    }


def _run_projection(run: Run) -> dict:
    return {
        "id": run.id,
        "session_id": run.session_id,
        "mode": run.mode,
        "status": run.status,
        "wait_reason": run.wait_reason,
        "root_work_item_id": run.root_work_item_id,
        "permission_mode": run.permission_mode,
        "terminal_reason": run.terminal_reason,
        "deadline_at": run.deadline_at.isoformat() if run.deadline_at else None,
        "version": run.version,
        "cancellation_requested_at": (
            run.cancellation_requested_at.isoformat()
            if run.cancellation_requested_at else None
        ),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


async def native_group_run_view(db, group_run: GroupRun) -> dict:
    run = await db.get(Run, group_run.run_id)
    members = list(await db.scalars(
        select(GroupRunMember).where(GroupRunMember.group_run_id == group_run.id)
        .order_by(GroupRunMember.agent_id)
    ))
    return {
        "id": group_run.id,
        "group_session_id": group_run.group_session_id,
        "reporter_agent_id": group_run.reporter_agent_id,
        "summary_epoch": group_run.summary_epoch,
        "delivered_summary_epoch": group_run.delivered_summary_epoch,
        "goal": group_run.goal,
        "shared_context": dict(group_run.shared_context or {}),
        "budget_snapshot": dict(group_run.budget_snapshot or {}),
        "policy_snapshot": dict(group_run.policy_snapshot or {}),
        "run": _run_projection(run),
        "members": [group_run_member_view(item) for item in members],
        "version": group_run.version,
    }


async def _next_group_message_sequence(db, group_session_id: str) -> int:
    # Serialize sequence allocation per Group so concurrent messages cannot
    # receive the same sequence number.
    await db.scalar(select(GroupSession.id).where(
        GroupSession.id == group_session_id,
    ).with_for_update())
    return int(await db.scalar(select(func.max(Message.sequence_no)).where(
        Message.group_session_id == group_session_id,
    )) or 0) + 1


def _member_runner(
    *, session: UserSession, group: GroupSession, group_run: GroupRun,
    membership: GroupMembership, snapshot: GroupRunMember, work: WorkItem,
    content: str, purpose: str = "group_message", summary_epoch: int | None = None,
) -> DurableRunner:
    return DurableRunner(
        id=uuid7(),
        work_item_id=work.id,
        run_id=work.run_id,
        session_id=work.session_id,
        agent_id=snapshot.agent_id,
        agent_version_id=snapshot.agent_version_id,
        execution_epoch=work.execution_epoch,
        status="queued",
        principal_snapshot={
            "user_id": session.user_id,
            "account_id": session.account_id,
            "session_id": session.id,
            "entry_agent_id": group.initiator_agent_id,
            "current_agent_id": snapshot.agent_id,
            "mode": "group",
            "permission_mode": work.mode == "group" and "direct" or session.permission_mode,
            "permission_revision": session.permission_revision,
            "group_session_id": group.id,
            "group_run_id": group_run.id,
            "group_membership_id": membership.id,
            # V1 field name is retained in the RuntimeContext but now carries
            # a Membership id, not a role/formation node.
            "group_membership_id": membership.id,
            "delegation_chain": [],
        },
        assembly_snapshot={"agent_version_id": snapshot.agent_version_id},
        prompt_manifest={},
        input_snapshot={
            "group_session_id": group.id,
            "group_run_id": group_run.id,
            "group_membership_id": membership.id,
            "group_membership_id": membership.id,
            "goal": content,
            "purpose": purpose,
            "summary_epoch": summary_epoch,
        },
    )


async def _create_native_group_run_graph(
    db, *, session: UserSession, group: GroupSession,
    memberships: list[GroupMembership], content: str,
    recipient_agent_id: str | None, reporter_agent_id: str | None,
    resolved_members: list[ResolvedGroupMember], shared_context_refs: list[str],
    event_key: str,
) -> GroupRun:
    by_agent = {item.agent_id: item for item in resolved_members}
    membership_by_agent = {item.agent_id: item for item in memberships}
    reporter_id = reporter_agent_id or group.initiator_agent_id or memberships[0].agent_id
    recipient_id = recipient_agent_id or reporter_id
    if reporter_id not in by_agent:
        raise GroupPolicyError("reporter_unavailable", "Reporter must be an available member")
    if recipient_id not in by_agent:
        raise GroupPolicyError("group_member_unavailable", "Recipient must be an available member")

    default_budget = default_run_budget("group")
    run = Run(
        id=uuid7(), session_id=session.id, user_id=session.user_id, mode="group",
        status="active",
        permission_mode="direct", hard_budget_snapshot=default_budget,
        entry_agent_id=recipient_id,
        deadline_at=deadline_from_budget(default_budget, mode="group"),
        started_at=utcnow(),
    )
    group_run = GroupRun(
        id=uuid7(), run_id=run.id, group_session_id=group.id,
        reporter_agent_id=reporter_id, goal=content,
        summary_epoch=0, delivered_summary_epoch=0,
        shared_context={"refs": list(shared_context_refs)},
        budget_snapshot=default_budget,
        policy_snapshot={
            "membership_policy": "fixed_for_run",
            "permission_mode": "direct",
            "message_visibility": ["direct", "group", "user"],
        },
    )
    db.add(run)
    await db.flush()
    db.add(group_run)
    await db.flush()

    snapshots: dict[str, GroupRunMember] = {}
    for member in memberships:
        resolved = by_agent[member.agent_id]
        collaboration = dict(resolved.card.collaboration_summary or {})
        context_scope = {
            "visibility": ["direct", "group", "user"],
            "shared_context_refs": list(shared_context_refs),
            "workspace_scope": "group_shared",
        }
        snapshot = GroupRunMember(
            group_run_id=group_run.id,
            agent_id=member.agent_id,
            membership_id=member.id,
            agent_version_id=resolved.version.id,
            participation_status="available",
            membership_policy="fixed_for_run",
            context_scope=context_scope,
            capability_boundary={
                "permission_mode": "direct",
                "agent_card_content_hash": resolved.card.content_hash,
                "agent_name": resolved.card.name,
                "accepted_request_summary": resolved.card.accepted_request_summary,
                "tool_keys": list(
                    (resolved.card.capabilities_summary or {}).get("tool_keys") or []
                ),
                "governance": collaboration.get("governance") or {},
            },
            budget_snapshot=dict(collaboration.get("budget") or default_budget),
        )
        snapshots[member.agent_id] = snapshot
        db.add(snapshot)
    await db.flush()

    target_membership = membership_by_agent[recipient_id]
    target_snapshot = snapshots[recipient_id]
    work = WorkItem(
        id=uuid7(), run_id=run.id, session_id=session.id, user_id=session.user_id,
        mode="group", owner_agent_id=recipient_id,
        agent_version_id=target_snapshot.agent_version_id,
        group_run_id=group_run.id, group_membership_id=target_membership.id,
        goal=content, purpose="group_message", status="queued",
        execution_epoch=1, attempt_no=1,
    )
    runner = _member_runner(
        session=session, group=group, group_run=group_run,
        membership=target_membership, snapshot=target_snapshot,
        work=work, content=content,
    )
    await persist_new_work_runner(db, work, runner)
    run.root_work_item_id = work.id

    message = Message(
        id=uuid7(), session_id=session.id, user_id=session.user_id,
        scope_type="group", group_session_id=group.id, group_run_id=group_run.id,
        sender_type="user", sender_user_id=session.user_id,
        recipient_type="agent", recipient_agent_id=recipient_id,
        recipient_membership_id=target_membership.id,
        visibility="direct", role="user", content=content,
        content_format="markdown", status="committed", run_id=run.id,
        work_item_id=work.id,
        sequence_no=await _next_group_message_sequence(db, group.id),
    )
    db.add(message)
    await db.flush()

    events = ExecutionEventWriter(db)
    created = await events.append(
        session_id=session.id, run_id=run.id,
        group_session_id=group.id, group_run_id=group_run.id,
        event_type="group.created" if event_key.startswith("create_group:") else "group.run_created",
        event_dedupe_key=f"{event_key}:run",
        actor_type="user", actor_id=session.user_id,
        entity_type="group_run", entity_id=group_run.id,
        entity_version=group_run.version, visibility="group",
        summary="Agent-native GroupRun created",
        payload={
            "member_agent_ids": [item.agent_id for item in memberships],
            "reporter_agent_id": reporter_id,
            "recipient_agent_id": recipient_id,
            "membership_policy": "fixed_for_run",
        },
        correlation_id=run.id,
    )
    message_event = await events.append(
        session_id=session.id, run_id=run.id,
        group_session_id=group.id, group_run_id=group_run.id,
        event_type="group.message_created",
        event_dedupe_key=f"{event_key}:message",
        actor_type="user", actor_id=session.user_id,
        entity_type="message", entity_id=message.id,
        visibility="direct", recipients=(
            EventRecipient("agent", recipient_id),
        ),
        summary="Directed Group user message created",
        payload={"message_id": message.id, "recipient_agent_id": recipient_id},
        correlation_id=run.id, caused_by_event_id=created.id,
    )
    queued = await events.append(
        session_id=session.id, run_id=run.id,
        group_session_id=group.id, group_run_id=group_run.id,
        event_type="runner.queued",
        event_dedupe_key=f"{event_key}:runner",
        actor_type="service", actor_id="group-runtime",
        entity_type="runner", entity_id=runner.id,
        entity_version=runner.version, visibility="private",
        private_owner=("runner", runner.id),
        summary="Group member Runner queued",
        payload={"agent_id": recipient_id, "membership_id": target_membership.id},
        correlation_id=run.id, caused_by_event_id=message_event.id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.runner", action="execute", aggregate_type="runner",
        aggregate_id=runner.id, partition_key=session.id,
        dedupe_key=f"event:{queued.id}:execute",
        payload_ref={"runner_id": runner.id, "group_run_id": group_run.id},
    )
    return group_run


async def create_group_with_initial_run(
    db, *, session: UserSession, account_id: str, member_agent_ids: list[str],
    goal: str, reporter_agent_id: str | None, shared_context_refs: list[str],
    event_key: str,
) -> tuple[GroupSession, GroupRun]:
    await db.scalar(select(UserSession.id).where(
        UserSession.id == session.id,
    ).with_for_update())
    members = await _resolve_group_members(
        db, account_id=account_id, agent_ids=member_agent_ids,
        actor_agent_id="__group_creation__",
    )
    initiator_id = reporter_agent_id or members[0].agent_id
    if initiator_id not in {item.agent_id for item in members}:
        raise GroupPolicyError("reporter_unavailable", "Reporter must be a selected member")
    group = GroupSession(
        id=uuid7(), session_id=session.id, user_id=session.user_id,
        initiator_agent_id=initiator_id,
        status="open", goal=goal, bootstrap_context="",
        membership_policy="fixed_for_run",
    )
    db.add(group)
    await db.flush()
    memberships: list[GroupMembership] = []
    for item in members:
        membership = GroupMembership(
            id=uuid7(), group_session_id=group.id, agent_id=item.agent_id,
            membership_no=1, status="joined", invited_by_kind="user",
            invited_by_account_id=account_id, reason="Selected directly from Agent Network",
            context_scope={
                "visibility": ["direct", "group", "user"],
                "shared_context_refs": list(shared_context_refs),
                "workspace_scope": "group_shared",
            },
            authorization_source="user_selected", joined_at=utcnow(),
        )
        memberships.append(membership)
        db.add(membership)
    await db.flush()
    group_run = await _create_native_group_run_graph(
        db, session=session, group=group, memberships=memberships,
        content=goal, recipient_agent_id=initiator_id,
        reporter_agent_id=initiator_id, resolved_members=members,
        shared_context_refs=shared_context_refs, event_key=event_key,
    )
    events = ExecutionEventWriter(db)
    for membership in memberships:
        await events.append(
            session_id=session.id, run_id=group_run.run_id,
            group_session_id=group.id, group_run_id=group_run.id,
            event_type="group.membership_joined",
            event_dedupe_key=f"{event_key}:membership:{membership.agent_id}",
            actor_type="user", actor_id=session.user_id,
            entity_type="group_membership", entity_id=membership.id,
            entity_version=membership.version, visibility="group",
            summary="User-selected Agent joined Group",
            payload={
                "agent_id": membership.agent_id,
                "authorization_source": membership.authorization_source,
            },
            correlation_id=group_run.run_id,
        )
    return group, group_run


async def create_followup_group_run(
    db, *, session: UserSession, group: GroupSession, account_id: str,
    content: str, recipient_agent_id: str | None,
    reporter_agent_id: str | None, reference_run_ids: list[str], event_key: str,
) -> GroupRun:
    await db.scalar(select(UserSession.id).where(
        UserSession.id == session.id,
    ).with_for_update())
    memberships = list(await db.scalars(select(GroupMembership).where(
        GroupMembership.group_session_id == group.id,
        GroupMembership.status == "joined",
        GroupMembership.ended_at.is_(None),
    ).order_by(GroupMembership.joined_at, GroupMembership.id)))
    if len(memberships) != 3:
        raise GroupPolicyError(
            "invalid_group_members", "Phase-one Group requires exactly three members"
        )
    resolved = await _resolve_group_members(
        db, account_id=account_id,
        agent_ids=[item.agent_id for item in memberships],
        actor_agent_id="__group_followup__",
    )
    if reference_run_ids:
        count = int(await db.scalar(select(func.count()).select_from(GroupRun).where(
            GroupRun.id.in_(set(reference_run_ids)),
            GroupRun.group_session_id == group.id,
        )) or 0)
        if count != len(set(reference_run_ids)):
            raise GroupPolicyError("invalid_group_run_reference", "Invalid GroupRun reference")
    return await _create_native_group_run_graph(
        db, session=session, group=group, memberships=memberships,
        content=content, recipient_agent_id=recipient_agent_id,
        reporter_agent_id=reporter_agent_id,
        resolved_members=resolved, shared_context_refs=list(dict.fromkeys(reference_run_ids)),
        event_key=event_key,
    )


async def create_user_group_message(
    db, *, session: UserSession, group: GroupSession, group_run: GroupRun,
    content: str, recipient_agent_id: str | None, event_key: str,
) -> tuple[Message, DurableRunner]:
    await db.scalar(select(UserSession.id).where(
        UserSession.id == session.id,
    ).with_for_update())
    run = await db.scalar(select(Run).where(Run.id == group_run.run_id).with_for_update())
    if run is None or run.status in {"completed", "cancelled", "failed"}:
        raise GroupPolicyError("group_run_not_accepting_messages", "GroupRun is terminal")
    target_id = recipient_agent_id or group_run.reporter_agent_id
    snapshot = await db.scalar(select(GroupRunMember).where(
        GroupRunMember.group_run_id == group_run.id,
        GroupRunMember.agent_id == target_id,
        GroupRunMember.participation_status == "available",
    ))
    if snapshot is None or not snapshot.agent_version_id:
        raise GroupPolicyError("group_member_unavailable", "Target member is unavailable")
    membership = await db.get(GroupMembership, snapshot.membership_id)
    work = WorkItem(
        id=uuid7(), run_id=run.id, session_id=session.id, user_id=session.user_id,
        mode="group", owner_agent_id=target_id,
        agent_version_id=snapshot.agent_version_id,
        group_run_id=group_run.id, group_membership_id=membership.id,
        goal=content, purpose="group_message", status="queued",
        execution_epoch=1, attempt_no=1,
    )
    runner = _member_runner(
        session=session, group=group, group_run=group_run,
        membership=membership, snapshot=snapshot, work=work, content=content,
    )
    await persist_new_work_runner(db, work, runner)
    message = Message(
        id=uuid7(), session_id=session.id, user_id=session.user_id,
        scope_type="group", group_session_id=group.id, group_run_id=group_run.id,
        sender_type="user", sender_user_id=session.user_id,
        recipient_type="agent", recipient_agent_id=target_id,
        recipient_membership_id=membership.id, visibility="direct",
        role="user", content=content, content_format="markdown",
        status="committed", run_id=run.id, work_item_id=work.id,
        sequence_no=await _next_group_message_sequence(db, group.id),
    )
    db.add(message)
    summary_requests = list(await db.scalars(select(GroupSummaryRequest).where(
        GroupSummaryRequest.group_run_id == group_run.id,
        GroupSummaryRequest.status.in_(("queued", "running")),
    ).with_for_update()))
    for request in summary_requests:
        request.status = "superseded"
        request.version += 1
    run.status = "active"
    run.wait_reason = None
    run.version += 1
    await db.flush()
    events = ExecutionEventWriter(db)
    created = await events.append(
        session_id=session.id, run_id=run.id,
        group_session_id=group.id, group_run_id=group_run.id,
        event_type="group.message_created", event_dedupe_key=f"{event_key}:message",
        actor_type="user", actor_id=session.user_id,
        entity_type="message", entity_id=message.id,
        visibility="direct", recipients=(EventRecipient("agent", target_id),),
        summary="User directly addressed a Group member",
        payload={"message_id": message.id, "recipient_agent_id": target_id},
        correlation_id=run.id,
    )
    queued = await events.append(
        session_id=session.id, run_id=run.id,
        group_session_id=group.id, group_run_id=group_run.id,
        event_type="runner.queued", event_dedupe_key=f"{event_key}:runner",
        actor_type="service", actor_id="group-runtime",
        entity_type="runner", entity_id=runner.id, entity_version=runner.version,
        visibility="private", private_owner=("runner", runner.id),
        summary="Directed Group member Runner queued",
        payload={"agent_id": target_id}, correlation_id=run.id,
        caused_by_event_id=created.id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.runner", action="execute", aggregate_type="runner",
        aggregate_id=runner.id, partition_key=session.id,
        dedupe_key=f"event:{queued.id}:execute",
        payload_ref={"runner_id": runner.id, "group_run_id": group_run.id},
    )
    return message, runner


async def send_agent_group_message(
    db, *, context, run_id: str, work_item_id: str, content: str,
    recipient_agent_id: str | None, visibility: str, operation_key: str,
) -> tuple[Message, DurableRunner | None]:
    """Persist an Agent message and wake only a direct recipient."""
    if visibility not in {"direct", "group"}:
        raise GroupPolicyError("domain_validation", "visibility must be direct or group")
    if visibility == "direct" and not recipient_agent_id:
        raise GroupPolicyError("domain_validation", "direct message requires recipient_agent_id")
    if visibility == "group" and recipient_agent_id:
        raise GroupPolicyError("domain_validation", "group message cannot set a recipient")
    run = await db.get(Run, run_id)
    work = await db.get(WorkItem, work_item_id)
    if run is None or work is None or run.mode != "group" or not work.group_run_id:
        raise GroupPolicyError("runtime_context_required", "Group runtime context is required")
    group_run = await db.get(GroupRun, work.group_run_id)
    group = await db.get(GroupSession, group_run.group_session_id) if group_run else None
    session = await db.get(UserSession, run.session_id)
    caller = await db.scalar(select(GroupRunMember).where(
        GroupRunMember.group_run_id == group_run.id,
        GroupRunMember.agent_id == context.agent_id,
        GroupRunMember.participation_status == "available",
    )) if group_run else None
    if group is None or session is None or caller is None:
        raise GroupPolicyError("group_member_unavailable", "Current Agent is not an available member")
    await db.scalar(select(GroupSession.id).where(
        GroupSession.id == group.id,
    ).with_for_update())
    existing = await db.scalar(select(Message).where(
        Message.group_run_id == group_run.id,
        Message.operation_key == operation_key,
    ))
    if existing is not None:
        return existing, None

    target_snapshot = None
    target_membership = None
    if recipient_agent_id:
        if recipient_agent_id == context.agent_id:
            raise GroupPolicyError("domain_validation", "Agent cannot directly message itself")
        target_snapshot = await db.scalar(select(GroupRunMember).where(
            GroupRunMember.group_run_id == group_run.id,
            GroupRunMember.agent_id == recipient_agent_id,
            GroupRunMember.participation_status == "available",
        ))
        if target_snapshot is None or not target_snapshot.agent_version_id:
            raise GroupPolicyError(
                "membership_policy_fixed_for_run",
                "Recipient is outside the fixed GroupRun membership snapshot",
            )
        target_membership = await db.get(GroupMembership, target_snapshot.membership_id)

    message = Message(
        id=uuid7(), session_id=run.session_id, user_id=run.user_id,
        scope_type="group", group_session_id=group.id, group_run_id=group_run.id,
        sender_type="agent", sender_agent_id=context.agent_id,
        recipient_type="agent" if recipient_agent_id else "group",
        recipient_agent_id=recipient_agent_id,
        recipient_membership_id=target_membership.id if target_membership else None,
        visibility=visibility, role="assistant", content=content,
        content_format="markdown", status="committed", run_id=run.id,
        work_item_id=work.id, operation_key=operation_key,
        sequence_no=await _next_group_message_sequence(db, group.id),
    )
    db.add(message)
    await db.flush()
    recipients = (
        (EventRecipient("agent", recipient_agent_id),) if recipient_agent_id else ()
    )
    created = await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id,
        group_session_id=group.id, group_run_id=group_run.id,
        event_type="group.message_created",
        event_dedupe_key=f"send_group_message:{operation_key}",
        actor_type="agent", actor_id=context.agent_id,
        entity_type="message", entity_id=message.id,
        visibility=visibility, recipients=recipients,
        summary="Agent sent a directed Group message" if recipient_agent_id else "Agent shared a Group message",
        payload={
            "message_id": message.id,
            "sender_agent_id": context.agent_id,
            "recipient_agent_id": recipient_agent_id,
            "visibility": visibility,
        },
        correlation_id=run.id,
    )
    if target_snapshot is None:
        return message, None
    target_work = WorkItem(
        id=uuid7(), run_id=run.id, session_id=run.session_id, user_id=run.user_id,
        mode="group", owner_agent_id=recipient_agent_id,
        agent_version_id=target_snapshot.agent_version_id,
        parent_work_item_id=work.id, group_run_id=group_run.id,
        group_membership_id=target_membership.id, goal=content,
        purpose="group_message", status="queued", execution_epoch=1, attempt_no=1,
    )
    target_runner = _member_runner(
        session=session, group=group, group_run=group_run,
        membership=target_membership, snapshot=target_snapshot,
        work=target_work, content=content,
    )
    target_runner.input_snapshot["source_message_id"] = message.id
    await persist_new_work_runner(db, target_work, target_runner)
    queued = await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id,
        group_session_id=group.id, group_run_id=group_run.id,
        event_type="runner.queued",
        event_dedupe_key=f"send_group_message:{operation_key}:runner",
        actor_type="service", actor_id="group-runtime",
        entity_type="runner", entity_id=target_runner.id,
        entity_version=target_runner.version, visibility="private",
        private_owner=("runner", target_runner.id),
        summary="Direct Group message woke its recipient",
        payload={"recipient_agent_id": recipient_agent_id, "message_id": message.id},
        correlation_id=run.id, caused_by_event_id=created.id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.runner", action="execute", aggregate_type="runner",
        aggregate_id=target_runner.id, partition_key=run.session_id,
        dedupe_key=f"event:{queued.id}:execute",
        payload_ref={"runner_id": target_runner.id, "group_run_id": group_run.id},
    )
    return message, target_runner


async def reject_fixed_run_invitation(
    db, *, context, run_id: str, work_item_id: str,
    target_agent_id: str, reason: str, operation_key: str,
) -> dict:
    run = await db.get(Run, run_id)
    work = await db.get(WorkItem, work_item_id)
    group_run = await db.get(GroupRun, work.group_run_id) if work and work.group_run_id else None
    if run is None or group_run is None or run.mode != "group":
        raise GroupPolicyError("runtime_context_required", "Group runtime context is required")
    current = await db.scalar(select(GroupRunMember).where(
        GroupRunMember.group_run_id == group_run.id,
        GroupRunMember.agent_id == target_agent_id,
    ))
    if current is not None:
        raise GroupPolicyError("domain_validation", "Agent is already a GroupRun member")
    event = await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id,
        group_session_id=group_run.group_session_id, group_run_id=group_run.id,
        event_type="group.membership_invitation_rejected",
        event_dedupe_key=f"invite_agent:{operation_key}:rejected",
        actor_type="agent", actor_id=context.agent_id,
        entity_type="group_run", entity_id=group_run.id,
        entity_version=group_run.version, visibility="group",
        summary="Out-of-graph invitation rejected by fixed_for_run policy",
        payload={
            "candidate_agent_id": target_agent_id,
            "invited_by_agent_id": context.agent_id,
            "reason": reason,
            "reason_code": "membership_policy_fixed_for_run",
            "result": "rejected",
        },
        correlation_id=run.id,
    )
    return {
        "event_id": event.id,
        "candidate_agent_id": target_agent_id,
        "status": "rejected",
        "reason_code": "membership_policy_fixed_for_run",
    }


async def update_runtime_shared_task(
    db, *, context, run_id: str, arguments: dict, operation_key: str,
) -> dict:
    run = await db.get(Run, run_id)
    group_run = await db.scalar(select(GroupRun).where(GroupRun.run_id == run_id)) if run else None
    if run is None or group_run is None or run.mode != "group":
        raise GroupPolicyError("runtime_context_required", "Group runtime context is required")
    prior_event = await db.scalar(select(ExecutionEvent).where(
        ExecutionEvent.session_id == run.session_id,
        ExecutionEvent.event_dedupe_key == f"update_shared_task:{operation_key}",
    ))
    if prior_event is not None:
        existing = await db.get(SharedTask, prior_event.entity_id)
        if existing is None:
            raise GroupPolicyError("not_found", "Idempotent SharedTask result was removed")
        return {
            "id": existing.id, "title": existing.title,
            "description": existing.description, "status": existing.status,
            "assignee_agent_id": existing.assignee_agent_id,
            "version": existing.version,
        }
    existing = await db.scalar(select(SharedTask).where(
        SharedTask.group_run_id == group_run.id,
        SharedTask.source_operation_key == operation_key,
    ))
    if existing is not None:
        return {
            "id": existing.id, "title": existing.title,
            "description": existing.description, "status": existing.status,
            "assignee_agent_id": existing.assignee_agent_id,
            "version": existing.version,
        }
    task_id = str(arguments.get("task_id") or "").strip()
    created = not task_id
    if task_id:
        task = await db.scalar(select(SharedTask).where(
            SharedTask.id == task_id,
            SharedTask.group_session_id == group_run.group_session_id,
        ).with_for_update())
        if task is None:
            raise GroupPolicyError("not_found", "SharedTask not found")
    else:
        title = str(arguments.get("title") or "").strip()
        if not title:
            raise GroupPolicyError("domain_validation", "title is required for a new SharedTask")
        task = SharedTask(
            id=uuid7(), group_session_id=group_run.group_session_id,
            group_run_id=group_run.id, title=title,
            description=str(arguments.get("description") or ""),
            status=str(arguments.get("status") or "pending"),
            assignee_agent_id=arguments.get("assignee_agent_id"),
            depends_on_task_ids=[], result_refs=[],
            source_operation_key=operation_key,
        )
        db.add(task)
    status = arguments.get("status")
    if status is not None:
        if status not in {"pending", "in_progress", "completed", "blocked", "cancelled"}:
            raise GroupPolicyError("domain_validation", "invalid SharedTask status")
        task.status = status
    for field in ("title", "description", "assignee_agent_id"):
        if field in arguments and arguments[field] is not None:
            setattr(task, field, arguments[field])
    if task.assignee_agent_id:
        assignee = await db.scalar(select(GroupRunMember.agent_id).where(
            GroupRunMember.group_run_id == group_run.id,
            GroupRunMember.agent_id == task.assignee_agent_id,
            GroupRunMember.participation_status == "available",
        ))
        if assignee is None:
            raise GroupPolicyError(
                "membership_policy_fixed_for_run",
                "SharedTask assignee is outside the fixed GroupRun snapshot",
            )
    if not created:
        task.version += 1
    await db.flush()
    await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id,
        group_session_id=group_run.group_session_id, group_run_id=group_run.id,
        event_type="shared_task.updated",
        event_dedupe_key=f"update_shared_task:{operation_key}",
        actor_type="agent", actor_id=context.agent_id,
        entity_type="shared_task", entity_id=task.id,
        entity_version=task.version, visibility="group",
        summary="Group SharedTask updated",
        payload={"task_id": task.id, "status": task.status, "assignee_agent_id": task.assignee_agent_id},
        correlation_id=run.id,
    )
    return {
        "id": task.id, "title": task.title, "description": task.description,
        "status": task.status, "assignee_agent_id": task.assignee_agent_id,
        "version": task.version,
    }
