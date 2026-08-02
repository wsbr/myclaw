"""Immutable Group Formation revision validation and projection."""

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.foundation import PlatformAccount
from app.models.group_v1 import (
    GroupFormationParticipant, GroupFormationProposal, GroupFormationRevision,
    GroupFormationSuggestedEdge, GroupFormationWorkItem, GroupSession,
)
from app.models.runtime_v1 import Run
from app.schemas.group_v1 import GroupInitialWorkItemInput, GroupParticipantInput, GroupSuggestedEdgeInput


class GroupFormationValidationError(ValueError):
    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("; ".join(issues))


async def validate_formation(
    db: AsyncSession, proposal: GroupFormationProposal, participants: list[GroupParticipantInput],
    work_items: list[GroupInitialWorkItemInput], edges: list[GroupSuggestedEdgeInput],
    *, resolve_versions: bool = False,
) -> dict[str, str]:
    issues: list[str] = []
    keys = [item.participant_key for item in participants]
    agent_ids = [item.agent_id for item in participants]
    if len(keys) != len(set(keys)): issues.append("participant_key values must be unique")
    if len(agent_ids) != len(set(agent_ids)): issues.append("participant Agents must be unique")
    entries = [item for item in participants if item.is_entry]
    if len(entries) != 1 or entries[0].agent_id != proposal.entry_agent_id:
        issues.append("exactly one entry participant must match the proposal entry Agent")
    key_set = set(keys)
    work_keys = [item.work_item_key for item in work_items]
    if len(work_keys) != len(set(work_keys)): issues.append("work_item_key values must be unique")
    for item in work_items:
        if item.participant_key not in key_set: issues.append(f"unknown Work Item participant: {item.participant_key}")
    edge_keys: set[tuple[str, str]] = set()
    for edge in edges:
        pair = (edge.source_participant_key, edge.target_participant_key)
        if pair[0] not in key_set or pair[1] not in key_set: issues.append(f"edge references unknown participant: {pair}")
        if pair[0] == pair[1]: issues.append(f"self edge is not allowed: {pair[0]}")
        if pair in edge_keys: issues.append(f"duplicate suggested edge: {pair}")
        edge_keys.add(pair)
    account = await db.scalar(select(PlatformAccount).where(PlatformAccount.user_id == proposal.user_id))
    versions: dict[str, str] = {}
    for agent_id in set(agent_ids):
        agent = await db.get(AgentBlueprint, agent_id)
        if agent is None or agent.status != "published" or not agent.current_published_version_id:
            issues.append(f"Agent is unavailable: {agent_id}")
            continue
        if agent.visibility == "private" and (account is None or agent.owner_account_id != account.id):
            issues.append(f"Agent is not accessible to this user: {agent_id}")
            continue
        version = await db.get(AgentAssemblyVersion, agent.current_published_version_id)
        if version is None or version.immutable_at is None or not version.group_enabled:
            issues.append(f"Agent is not enabled for Group: {agent_id}")
        elif resolve_versions:
            versions[agent_id] = version.id
    if issues: raise GroupFormationValidationError(issues)
    return versions


async def create_revision(
    db: AsyncSession, proposal: GroupFormationProposal, *, goal: str, bootstrap_context: str,
    participants: list[GroupParticipantInput], work_items: list[GroupInitialWorkItemInput],
    edges: list[GroupSuggestedEdgeInput], source: str, edited_by_type: str, edited_by_id: str,
) -> GroupFormationRevision:
    await validate_formation(db, proposal, participants, work_items, edges)
    payload = {"goal": goal.strip(), "bootstrap_context": bootstrap_context.strip(),
               "participants": [item.model_dump(mode="json") for item in participants],
               "initial_work_items": [item.model_dump(mode="json") for item in work_items],
               "suggested_edges": [item.model_dump(mode="json") for item in edges]}
    revision = GroupFormationRevision(
        proposal_id=proposal.id, revision_no=proposal.current_revision_no + 1, source=source,
        derived_from_revision_id=proposal.current_revision_id, goal=goal.strip(),
        bootstrap_context=bootstrap_context.strip(),
        content_hash=hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        edited_by_type=edited_by_type, edited_by_id=edited_by_id,
    )
    db.add(revision)
    await db.flush()
    by_key: dict[str, GroupFormationParticipant] = {}
    for order, item in enumerate(participants):
        row = GroupFormationParticipant(revision_id=revision.id, participant_key=item.participant_key,
                                        agent_id=item.agent_id, is_entry=item.is_entry,
                                        initial_responsibility=item.initial_responsibility.strip(), sort_order=order)
        db.add(row); by_key[item.participant_key] = row
    await db.flush()
    for order, item in enumerate(work_items):
        db.add(GroupFormationWorkItem(revision_id=revision.id, work_item_key=item.work_item_key,
                                      participant_id=by_key[item.participant_key].id, title=item.title.strip(),
                                      goal=item.goal.strip(), acceptance_criteria=item.acceptance_criteria.strip(),
                                      sort_order=order))
    for order, item in enumerate(edges):
        db.add(GroupFormationSuggestedEdge(revision_id=revision.id,
                                           source_participant_id=by_key[item.source_participant_key].id,
                                           target_participant_id=by_key[item.target_participant_key].id,
                                           label=item.label.strip(), rationale=item.rationale.strip(), sort_order=order))
    proposal.current_revision_id = revision.id
    proposal.current_revision_no = revision.revision_no
    proposal.status = "awaiting_confirmation"
    proposal.version += 1
    await db.flush()
    return revision


async def revision_view(db: AsyncSession, revision: GroupFormationRevision) -> dict:
    participants = list(await db.scalars(select(GroupFormationParticipant).where(
        GroupFormationParticipant.revision_id == revision.id).order_by(GroupFormationParticipant.sort_order)))
    by_id = {row.id: row.participant_key for row in participants}
    work_items = list(await db.scalars(select(GroupFormationWorkItem).where(
        GroupFormationWorkItem.revision_id == revision.id).order_by(GroupFormationWorkItem.sort_order)))
    edges = list(await db.scalars(select(GroupFormationSuggestedEdge).where(
        GroupFormationSuggestedEdge.revision_id == revision.id).order_by(GroupFormationSuggestedEdge.sort_order)))
    return {"id": revision.id, "revision_no": revision.revision_no, "source": revision.source,
            "goal": revision.goal, "bootstrap_context": revision.bootstrap_context,
            "content_hash": revision.content_hash,
            "participants": [{"id": row.id, "participant_key": row.participant_key, "agent_id": row.agent_id,
                              "is_entry": row.is_entry, "initial_responsibility": row.initial_responsibility,
                              "sort_order": row.sort_order} for row in participants],
            "initial_work_items": [{"id": row.id, "work_item_key": row.work_item_key,
                                    "participant_key": by_id[row.participant_id], "title": row.title,
                                    "goal": row.goal, "acceptance_criteria": row.acceptance_criteria,
                                    "sort_order": row.sort_order} for row in work_items],
            "suggested_edges": [{"id": row.id, "source_participant_key": by_id[row.source_participant_id],
                                 "target_participant_key": by_id[row.target_participant_id], "label": row.label,
                                 "rationale": row.rationale, "sort_order": row.sort_order} for row in edges]}


async def proposal_view(db: AsyncSession, proposal: GroupFormationProposal) -> dict:
    group = await db.get(GroupSession, proposal.group_session_id)
    run = await db.get(Run, proposal.generation_run_id)
    revision = await db.get(GroupFormationRevision, proposal.current_revision_id) if proposal.current_revision_id else None
    return {"id": proposal.id, "parent_session_id": proposal.parent_session_id,
            "group_session_id": proposal.group_session_id, "entry_agent_id": proposal.entry_agent_id,
            "status": proposal.status, "current_revision_no": proposal.current_revision_no,
            "version": proposal.version, "group_session": {"id": group.id, "status": group.status,
            "version": group.version}, "generation_run": {"id": run.id, "lifecycle": run.lifecycle,
            "outcome": run.outcome, "version": run.version},
            "current_revision": await revision_view(db, revision) if revision else None}
