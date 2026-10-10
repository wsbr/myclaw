"""Agent-native Group creation, messaging, snapshots, and projections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import func, select

from app.config import settings
from app.models.agent_v1 import AgentAssemblyVersion, AgentCard
from app.models.common import utcnow, uuid7
from app.models.foundation import ExecutionEvent, UserSession
from app.models.group_v1 import (
    GroupMembership,
    GroupRun,
    GroupRunMember,
    GroupRunReference,
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
from app.services.agent_directory_service import discover_agent_cards, resolve_callable_agent
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


def _group_card_is_eligible(card: AgentCard) -> bool:
    collaboration = dict(card.collaboration_summary or {})
    governance = collaboration.get("governance") or {}
    return not (
        collaboration.get("callable") is False
        or (
            isinstance(governance, dict)
            and (
                governance.get("callable") is False
                or governance.get("group_allowed") is False
            )
        )
    )


async def resolve_group_emergence_anchor(
    db, *, tenant_id: str, account_id: str | None,
    agent_id: str, agent_version_id: str,
) -> ResolvedGroupMember:
    """Require the Chat's frozen Agent to remain eligible to join its Group."""

    version = await db.get(AgentAssemblyVersion, agent_version_id)
    if (
        version is None or version.agent_id != agent_id
        or version.publication_status != "published"
        or version.immutable_at is None
    ):
        raise GroupPolicyError(
            "group_initiator_unavailable", "The Chat Agent version is unavailable for Group"
        )
    if not version.group_enabled:
        raise GroupPolicyError(
            "group_initiator_not_network_enabled",
            "The Chat Agent must enable Group collaboration before selecting members",
        )
    try:
        agent, card = await resolve_callable_agent(
            db, tenant_id=tenant_id, account_id=account_id,
            actor_agent_id="__group_emergence__", target_agent_id=agent_id,
        )
    except (LookupError, PermissionError) as error:
        raise GroupPolicyError("group_initiator_unavailable", str(error)) from error
    if card.agent_version_id != version.id:
        raise GroupPolicyError(
            "group_initiator_snapshot_changed",
            "The Chat Agent publication changed; start a new Chat before forming a Group",
        )
    collaboration = dict(card.collaboration_summary or {})
    governance = collaboration.get("governance") or {}
    if (
        not card.group_enabled or not _group_card_is_eligible(card)
        or collaboration.get("discoverable") is False
        or (isinstance(governance, dict) and governance.get("discoverable") is False)
    ):
        raise GroupPolicyError(
            "group_initiator_not_network_enabled",
            "The Chat Agent is not currently eligible for Group collaboration",
        )
    return ResolvedGroupMember(agent.id, version, card)


async def build_group_emergence_candidate_snapshot(
    db, *, tenant_id: str, account_id: str | None,
    current_agent_id: str, current_agent_version_id: str,
) -> dict:
    """Freeze the complete model-facing semantic candidate catalog for Chat.

    Only Agent identity data needed for semantic selection enters the prompt.
    A catalog that exceeds the configured bound is rejected as a whole rather
    than truncated, because a partial catalog would bias the model's choice.
    """

    try:
        anchor = await resolve_group_emergence_anchor(
            db, tenant_id=tenant_id, account_id=account_id,
            agent_id=current_agent_id, agent_version_id=current_agent_version_id,
        )
    except GroupPolicyError as error:
        return {
            "status": "unavailable", "reason": error.code,
            "candidate_count": 0, "current_chat_agent_id": current_agent_id,
            "candidates": [], "candidate_versions": {},
        }

    cards = await discover_agent_cards(
        db,
        tenant_id=tenant_id,
        account_id=account_id,
        actor_agent_id=current_agent_id,
        actor_agent_version_id=current_agent_version_id,
        group_enabled=True,
        limit=500,
        exhaustive=True,
    )
    cards = [anchor.card, *cards]
    eligible = [card for card in cards if _group_card_is_eligible(card)]
    # Directory discovery has a hard defensive ceiling of 500. Reaching it
    # means completeness cannot be proven, so do not let the model choose from
    # what may be a biased prefix.
    catalog_completeness_unknown = len(cards) >= 500
    if (
        catalog_completeness_unknown
        or len(eligible) > settings.emergent_group_max_candidates
    ):
        return {
            "status": "unavailable",
            "reason": (
                "group_candidate_catalog_completeness_unknown"
                if catalog_completeness_unknown
                else "group_candidate_catalog_limit_exceeded"
            ),
            "candidate_count": len(eligible),
            "max_candidates": settings.emergent_group_max_candidates,
            "current_chat_agent_id": current_agent_id,
            "candidates": [],
        }
    candidates = [{
        "agent_id": card.agent_id,
        "name": card.name,
        "description": card.description,
    } for card in eligible]
    candidate_versions = {
        card.agent_id: {
            "agent_version_id": card.agent_version_id,
            "card_content_hash": card.content_hash,
        }
        for card in eligible
    }
    encoded = json.dumps(
        candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    catalog_hash = hashlib.sha256(encoded.encode()).hexdigest()
    if len(candidates) < 2:
        return {
            "status": "unavailable",
            "reason": "insufficient_group_candidates",
            "candidate_count": len(candidates),
            "max_candidates": settings.emergent_group_max_candidates,
            "current_chat_agent_id": current_agent_id,
            "catalog_hash": catalog_hash,
            "candidates": candidates,
            "candidate_versions": candidate_versions,
        }
    return {
        "status": "ready",
        "candidate_count": len(candidates),
        "max_candidates": settings.emergent_group_max_candidates,
        "current_chat_agent_id": current_agent_id,
        "catalog_hash": catalog_hash,
        "candidates": candidates,
        "candidate_versions": candidate_versions,
    }


async def _resolve_group_members(
    db, *, tenant_id: str, account_id: str,
    agent_ids: Iterable[str], actor_agent_id: str,
) -> list[ResolvedGroupMember]:
    normalized = [str(item).strip() for item in agent_ids if str(item).strip()]
    if len(normalized) < 2 or len(set(normalized)) != len(normalized):
        raise GroupPolicyError(
            "invalid_group_members", "Group requires at least 2 unique Agent IDs"
        )
    if len(normalized) > settings.emergent_group_max_members:
        raise GroupPolicyError(
            "group_member_limit_exceeded",
            "Enabled Group Agent count exceeds the configured safe bound",
            details={
                "member_count": len(normalized),
                "max_members": settings.emergent_group_max_members,
            },
        )
    resolved: list[ResolvedGroupMember] = []
    for agent_id in normalized:
        try:
            agent, card = await resolve_callable_agent(
                db,
                tenant_id=tenant_id,
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
        "tenant_id": group.tenant_id,
        "session_id": group.session_id,
        "status": group.status,
        "initiator_agent_id": group.initiator_agent_id,
        "membership_policy": group.membership_policy,
        "formation_mode": group.formation_mode,
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
        "tenant_id": run.tenant_id,
        "account_id": run.account_id,
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
        tenant_id=session.tenant_id,
        work_item_id=work.id,
        run_id=work.run_id,
        session_id=work.session_id,
        agent_id=snapshot.agent_id,
        agent_version_id=snapshot.agent_version_id,
        execution_epoch=work.execution_epoch,
        status="queued",
        principal_snapshot={
            "tenant_id": session.tenant_id,
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
            "delegation_chain": [],
        },
        assembly_snapshot={"agent_version_id": snapshot.agent_version_id},
        prompt_manifest={},
        input_snapshot={
            "group_session_id": group.id,
            "group_run_id": group_run.id,
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
    event_key: str, shared_context: dict | None = None,
    created_event_type: str | None = None,
    created_actor_type: str = "user", created_actor_id: str | None = None,
    created_summary: str = "Agent-native GroupRun created",
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
        id=uuid7(), tenant_id=session.tenant_id, account_id=session.account_id,
        session_id=session.id, user_id=session.user_id, mode="group",
        status="active",
        permission_mode="direct", hard_budget_snapshot=default_budget,
        deadline_at=deadline_from_budget(default_budget, mode="group"),
    )
    group_run = GroupRun(
        id=uuid7(), run_id=run.id, group_session_id=group.id,
        reporter_agent_id=reporter_id, goal=content,
        summary_epoch=0, delivered_summary_epoch=0,
        shared_context={
            **dict(shared_context or {}),
            "refs": list(shared_context_refs),
        },
        budget_snapshot=default_budget,
        policy_snapshot={
            "membership_policy": "fixed_for_run",
            "formation_mode": group.formation_mode,
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
                "agent_description": resolved.card.description,
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
        id=uuid7(), tenant_id=session.tenant_id,
        run_id=run.id, session_id=session.id, user_id=session.user_id,
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
    runner.input_snapshot = {
        **dict(runner.input_snapshot or {}),
        "message_id": message.id,
    }
    await db.flush()

    events = ExecutionEventWriter(db)
    created = await events.append(
        session_id=session.id, run_id=run.id,
        group_session_id=group.id, group_run_id=group_run.id,
        event_type=(
            created_event_type
            or ("group.created" if event_key.startswith("create_group:") else "group.run_created")
        ),
        event_dedupe_key=f"{event_key}:run",
        actor_type=created_actor_type,
        actor_id=created_actor_id or session.user_id,
        entity_type="group_run", entity_id=group_run.id,
        entity_version=group_run.version, visibility="group",
        summary=created_summary,
        payload={
            "group_session_id": group.id,
            "group_run_id": group_run.id,
            "member_agent_ids": [item.agent_id for item in memberships],
            "reporter_agent_id": reporter_id,
            "recipient_agent_id": recipient_id,
            "membership_policy": "fixed_for_run",
            "formation_mode": group.formation_mode,
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
            EventRecipient("user_identity", session.user_id),
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
        tenant_id=session.tenant_id, account_id=session.account_id,
        payload_ref={"runner_id": runner.id, "group_run_id": group_run.id},
    )
    return group_run


async def create_group_with_initial_run(
    db, *, session: UserSession, account_id: str, member_agent_ids: list[str],
    goal: str, reporter_agent_id: str | None, shared_context_refs: list[str],
    event_key: str,
) -> tuple[GroupSession, GroupRun]:
    if len(member_agent_ids) != 3 or len(set(member_agent_ids)) != 3:
        raise GroupPolicyError(
            "invalid_group_members", "Manual Group requires exactly 3 unique Agent IDs"
        )
    await db.scalar(select(UserSession.id).where(
        UserSession.id == session.id,
    ).with_for_update())
    members = await _resolve_group_members(
        db, tenant_id=session.tenant_id, account_id=account_id,
        agent_ids=member_agent_ids,
        actor_agent_id="__group_creation__",
    )
    initiator_id = reporter_agent_id or members[0].agent_id
    if initiator_id not in {item.agent_id for item in members}:
        raise GroupPolicyError("reporter_unavailable", "Reporter must be a selected member")
    group = GroupSession(
        id=uuid7(), tenant_id=session.tenant_id,
        session_id=session.id, user_id=session.user_id,
        initiator_agent_id=initiator_id,
        status="open", goal=goal, bootstrap_context="",
        membership_policy="fixed_for_run", formation_mode="manual_selected",
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


async def emerge_group_after_chat(
    db, *, source_run_id: str, source_runner_id: str,
) -> tuple[GroupSession, GroupRun] | None:
    """Apply the Chat model's semantic member selection after Chat completes.

    Formation is a durable, idempotent follow-up. The Chat transaction remains
    successful when the model selected no Group or a selected Agent became
    ineligible. Membership may change between emergent Runs, while every
    created GroupRun keeps its own immutable member/version snapshot.
    """

    source_run = await db.scalar(select(Run).where(
        Run.id == source_run_id,
    ).with_for_update())
    source_runner = await db.get(DurableRunner, source_runner_id)
    if (
        source_run is None
        or source_runner is None
        or source_runner.run_id != source_run.id
        or source_run.mode != "chat"
        or source_run.status != "completed"
    ):
        return None
    source_work = await db.get(WorkItem, source_runner.work_item_id)
    if (
        source_work is None or source_work.id != source_run.root_work_item_id
        or source_work.run_id != source_run.id or source_work.parent_work_item_id is not None
        or source_work.owner_agent_id != source_runner.agent_id
        or source_work.agent_version_id != source_runner.agent_version_id
    ):
        return None
    session = await db.scalar(select(UserSession).where(
        UserSession.id == source_run.session_id,
    ).with_for_update())
    if session is None or session.status != "active":
        return None
    event_key = f"emerge_group_after_chat:{source_run.id}"
    prior = await db.scalar(select(ExecutionEvent).where(
        ExecutionEvent.session_id == session.id,
        ExecutionEvent.event_dedupe_key == f"{event_key}:run",
    ))
    if prior is not None and prior.entity_type == "group_run":
        prior_run = await db.get(GroupRun, prior.entity_id)
        prior_group = (
            await db.get(GroupSession, prior_run.group_session_id)
            if prior_run is not None else None
        )
        if prior_group is not None:
            return prior_group, prior_run

    selection = await db.scalar(select(ExecutionEvent).where(
        ExecutionEvent.session_id == session.id,
        ExecutionEvent.run_id == source_run.id,
        ExecutionEvent.event_type == "group.emergence_selected",
        ExecutionEvent.event_dedupe_key == f"group-emergence-selection:{source_run.id}",
    ))
    selected_ids = [
        str(item).strip()
        for item in (
            ((selection.payload or {}).get("selected_agent_ids") or [])
            if selection else []
        )
        if str(item).strip()
    ]
    frozen_emergence = dict(
        (source_runner.input_snapshot or {}).get("group_emergence") or {}
    )
    frozen_candidate_versions = dict(
        frozen_emergence.get("candidate_versions") or {}
    )
    skip_reason = None
    if selection is None:
        skip_reason = "model_selected_no_group"
    else:
        try:
            anchor = await resolve_group_emergence_anchor(
                db, tenant_id=session.tenant_id, account_id=session.account_id,
                agent_id=source_runner.agent_id,
                agent_version_id=source_runner.agent_version_id,
            )
        except GroupPolicyError as error:
            skip_reason = error.code
        else:
            frozen_anchor = dict(frozen_candidate_versions.get(source_runner.agent_id) or {})
            if (
                frozen_emergence.get("current_chat_agent_id") != source_runner.agent_id
                or frozen_anchor.get("agent_version_id") != source_runner.agent_version_id
                or frozen_anchor.get("card_content_hash") != anchor.card.content_hash
            ):
                skip_reason = "group_initiator_snapshot_changed"
            elif source_runner.agent_id not in selected_ids:
                # Preserve the initiating Agent even for an older queued selection.
                selected_ids = [source_runner.agent_id, *selected_ids]
    if skip_reason is None and (
        len(selected_ids) < 2 or len(selected_ids) != len(set(selected_ids))
    ):
        skip_reason = "invalid_model_group_selection"
    elif skip_reason is None and len(selected_ids) > settings.emergent_group_max_members:
        skip_reason = "group_member_limit_exceeded"
    if skip_reason is not None:
        await ExecutionEventWriter(db).append(
            session_id=session.id,
            run_id=source_run.id,
            event_type="group.emergence_skipped",
            event_dedupe_key=f"{event_key}:skipped",
            actor_type="service",
            actor_id="group-emergence",
            entity_type="run",
            entity_id=source_run.id,
            entity_version=source_run.version,
            visibility="user",
            summary="Automatic Group emergence was safely skipped",
            payload={
                "source_chat_run_id": source_run.id,
                "reason": skip_reason,
                "selected_member_count": len(selected_ids),
                "max_members": settings.emergent_group_max_members,
            },
            correlation_id=source_run.id,
        )
        return None

    try:
        resolved = await _resolve_group_members(
            db,
            tenant_id=session.tenant_id,
            account_id=session.account_id,
            agent_ids=selected_ids,
            actor_agent_id="__group_emergence__",
        )
    except GroupPolicyError as error:
        await ExecutionEventWriter(db).append(
            session_id=session.id,
            run_id=source_run.id,
            event_type="group.emergence_skipped",
            event_dedupe_key=f"{event_key}:skipped",
            actor_type="service",
            actor_id="group-emergence",
            entity_type="run",
            entity_id=source_run.id,
            entity_version=source_run.version,
            visibility="user",
            summary="Automatic Group emergence was safely skipped",
            payload={
                "source_chat_run_id": source_run.id,
                "reason": "selected_member_no_longer_eligible",
                "policy_error_code": error.code,
                "selected_member_count": len(selected_ids),
            },
            correlation_id=source_run.id,
            caused_by_event_id=selection.id,
        )
        return None
    selected_versions = dict(
        (selection.payload or {}).get("selected_agent_versions") or {}
    )
    selected_versions = {
        agent_id: dict(
            selected_versions.get(agent_id)
            or frozen_candidate_versions.get(agent_id)
            or {}
        )
        for agent_id in selected_ids
    }
    changed_ids = [
        item.agent_id
        for item in resolved
        if (
            str((selected_versions.get(item.agent_id) or {}).get("agent_version_id") or "")
            != item.version.id
            or str((selected_versions.get(item.agent_id) or {}).get("card_content_hash") or "")
            != item.card.content_hash
        )
    ]
    if changed_ids:
        await ExecutionEventWriter(db).append(
            session_id=session.id,
            run_id=source_run.id,
            event_type="group.emergence_skipped",
            event_dedupe_key=f"{event_key}:skipped",
            actor_type="service",
            actor_id="group-emergence",
            entity_type="run",
            entity_id=source_run.id,
            entity_version=source_run.version,
            visibility="user",
            summary="Automatic Group emergence was safely skipped",
            payload={
                "source_chat_run_id": source_run.id,
                "reason": "selected_member_snapshot_changed",
                "changed_agent_ids": changed_ids,
            },
            correlation_id=source_run.id,
            caused_by_event_id=selection.id,
        )
        return None
    reporter_id = source_runner.agent_id
    group = await db.scalar(select(GroupSession).where(
        GroupSession.session_id == session.id,
        GroupSession.status == "open",
        GroupSession.formation_mode == "emergent_model_selected",
    ).order_by(GroupSession.created_at.desc(), GroupSession.id.desc()).limit(1))
    created_group = group is None

    source_messages = list(await db.scalars(select(Message).where(
        Message.run_id == source_run.id,
        Message.scope_type == "chat",
        Message.status == "committed",
    ).order_by(Message.created_at, Message.id)))
    user_messages = [item for item in source_messages if item.sender_type == "user"]
    assistant_messages = [item for item in source_messages if item.sender_type == "agent"]
    source_user = user_messages[-1] if user_messages else None
    source_assistant = (
        await db.get(Message, source_runner.result_message_id)
        if source_runner.result_message_id else None
    )
    if source_assistant is None and assistant_messages:
        source_assistant = assistant_messages[-1]
    # Scheduled directives use sender_type=system so memory provenance stays
    # user-only. Their root WorkItem remains the canonical collaboration goal.
    goal = (
        source_user.content
        if source_user is not None
        else source_work.goal if source_work is not None
        else "Review the completed Chat response"
    )

    if group is None:
        group = GroupSession(
            id=uuid7(),
            tenant_id=session.tenant_id,
            session_id=session.id,
            user_id=session.user_id,
            initiator_agent_id=reporter_id,
            status="open",
            goal=goal,
            bootstrap_context=(source_assistant.content if source_assistant else ""),
            membership_policy="dynamic_future",
            formation_mode="emergent_model_selected",
        )
        db.add(group)
        await db.flush()

    all_memberships = list(await db.scalars(select(GroupMembership).where(
        GroupMembership.group_session_id == group.id,
    ).order_by(GroupMembership.agent_id, GroupMembership.membership_no)))
    joined_by_agent = {
        item.agent_id: item for item in all_memberships
        if item.status == "joined" and item.ended_at is None
    }
    selected_set = {item.agent_id for item in resolved}
    removed: list[GroupMembership] = []
    for agent_id, membership in list(joined_by_agent.items()):
        if agent_id in selected_set:
            continue
        membership.status = "removed"
        membership.ended_at = utcnow()
        membership.reason = "Agent was not selected for the next emergent GroupRun"
        membership.version += 1
        removed.append(membership)
        del joined_by_agent[agent_id]

    joined: list[GroupMembership] = []
    membership_by_agent: dict[str, GroupMembership] = dict(joined_by_agent)
    for member in resolved:
        if member.agent_id in membership_by_agent:
            continue
        prior_numbers = [
            item.membership_no for item in all_memberships
            if item.agent_id == member.agent_id
        ]
        membership = GroupMembership(
            id=uuid7(),
            group_session_id=group.id,
            agent_id=member.agent_id,
            membership_no=max(prior_numbers, default=0) + 1,
            status="joined",
            invited_by_kind="system",
            invited_by_account_id=session.account_id,
            reason="Selected by the Chat model from the governed Group candidate catalog",
            context_scope={
                "visibility": ["direct", "group", "user"],
                "shared_context_refs": [],
                "workspace_scope": "group_shared",
            },
            authorization_source="model_selected_group_candidate",
            joined_at=utcnow(),
        )
        db.add(membership)
        joined.append(membership)
        membership_by_agent[member.agent_id] = membership
    if joined or removed:
        group.version += 1
    await db.flush()

    active_memberships = [membership_by_agent[item.agent_id] for item in resolved]
    group_run = await _create_native_group_run_graph(
        db,
        session=session,
        group=group,
        memberships=active_memberships,
        content=goal,
        recipient_agent_id=reporter_id,
        reporter_agent_id=reporter_id,
        resolved_members=resolved,
        shared_context_refs=[],
        shared_context={
            "formation_mode": "emergent_model_selected",
            "conversation_anchor_agent_id": source_runner.agent_id,
            "selection_event_id": selection.id,
            "selection_rationale": str((selection.payload or {}).get("rationale") or ""),
            "candidate_catalog_hash": str(
                (selection.payload or {}).get("candidate_catalog_hash") or ""
            ),
            "source_chat_run_id": source_run.id,
            "source_chat_message_id": source_user.id if source_user else None,
            "source_chat_response_message_id": (
                source_assistant.id if source_assistant else None
            ),
            "source_chat_response": (
                source_assistant.content if source_assistant else None
            ),
        },
        event_key=event_key,
        created_event_type=("group.emerged" if created_group else "group.run_emerged"),
        created_actor_type="service",
        created_actor_id="group-emergence",
        created_summary=(
            "The Chat model's selected Agents emerged after Chat completion"
            if created_group
            else "A new model-selected emergent GroupRun followed Chat completion"
        ),
    )
    events = ExecutionEventWriter(db)
    for membership in joined:
        await events.append(
            session_id=session.id,
            run_id=group_run.run_id,
            group_session_id=group.id,
            group_run_id=group_run.id,
            event_type="group.membership_joined",
            event_dedupe_key=f"{event_key}:membership:joined:{membership.id}",
            actor_type="service",
            actor_id="group-emergence",
            entity_type="group_membership",
            entity_id=membership.id,
            entity_version=membership.version,
            visibility="group",
            summary="Model-selected Agent joined the emergent Group",
            payload={
                "agent_id": membership.agent_id,
                "authorization_source": membership.authorization_source,
            },
            correlation_id=group_run.run_id,
        )
    for membership in removed:
        await events.append(
            session_id=session.id,
            run_id=group_run.run_id,
            group_session_id=group.id,
            group_run_id=group_run.id,
            event_type="group.membership_removed",
            event_dedupe_key=f"{event_key}:membership:removed:{membership.id}",
            actor_type="service",
            actor_id="group-emergence",
            entity_type="group_membership",
            entity_id=membership.id,
            entity_version=membership.version,
            visibility="group",
            summary="Agent left active membership after Chat model reselection",
            payload={"agent_id": membership.agent_id},
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
    required_count = 3 if group.formation_mode == "manual_selected" else 2
    if len(memberships) < required_count or (
        group.formation_mode == "manual_selected" and len(memberships) != 3
    ):
        raise GroupPolicyError(
            "invalid_group_members",
            "Group no longer has enough active members for a new GroupRun",
        )
    resolved = await _resolve_group_members(
        db, tenant_id=session.tenant_id, account_id=account_id,
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
    group_run = await _create_native_group_run_graph(
        db, session=session, group=group, memberships=memberships,
        content=content, recipient_agent_id=recipient_agent_id,
        reporter_agent_id=reporter_agent_id,
        resolved_members=resolved, shared_context_refs=list(dict.fromkeys(reference_run_ids)),
        event_key=event_key,
    )
    for referenced_id in dict.fromkeys(reference_run_ids):
        db.add(GroupRunReference(
            group_run_id=group_run.id,
            referenced_group_run_id=referenced_id,
        ))
    await db.flush()
    return group_run


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
        id=uuid7(), tenant_id=session.tenant_id,
        run_id=run.id, session_id=session.id, user_id=session.user_id,
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
    runner.input_snapshot = {
        **dict(runner.input_snapshot or {}),
        "message_id": message.id,
    }
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
        visibility="direct", recipients=(
            EventRecipient("agent", target_id),
            EventRecipient("user_identity", session.user_id),
        ),
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
        tenant_id=session.tenant_id, account_id=session.account_id,
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
    if visibility == "group" and len(content) > 800:
        raise GroupPolicyError(
            "domain_validation",
            "group history notes must be concise (at most 800 characters)",
        )
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
        (
            EventRecipient("agent", recipient_agent_id),
            EventRecipient("user_identity", run.user_id),
        )
        if recipient_agent_id else ()
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
        id=uuid7(), tenant_id=run.tenant_id,
        run_id=run.id, session_id=run.session_id, user_id=run.user_id,
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
        tenant_id=run.tenant_id, account_id=run.account_id,
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
