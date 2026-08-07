from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.sessions import _idempotency, _owned_session
from app.database import get_db
from app.deps import SessionActor, get_session_actor
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import ExecutionEvent, UserSession
from app.models.group_v1 import (
    GroupAgentNode, GroupCollaborationEdge, GroupFormationParticipant, GroupFormationProposal, GroupFormationRevision,
    GroupFormationSuggestedEdge, GroupFormationWorkItem, GroupInitialSuggestedEdge,
    GroupRun, GroupRunAgentVersion, GroupRunReference, GroupSession,
    GroupMembership, GroupRunMember, GroupSummaryRequest, SharedTask,
)
from app.models.runtime_v1 import (
    AgentInvocation, Clarification, DurableRunner, Message, Run, ToolConfirmation,
    ToolInvocation, WorkItem,
)
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_new_work_runner
from app.services.notification_delivery_service import create_notification_delivery_if_channel
from app.schemas.group_v1 import (
    GroupFormationConfirmRequest, GroupFormationCreateRequest, GroupFormationPatchRequest,
    GroupCloseRequest, GroupInitialWorkItemInput, GroupParticipantInput, GroupRunCreateRequest,
    GroupRunMessageRequest, GroupSuggestedEdgeInput, GroupCreateRequest,
    GroupReporterReplaceRequest, SharedTaskCreateRequest, SharedTaskPatchRequest,
)
from app.services.group_formation_service import (
    GroupFormationValidationError, create_revision, proposal_view, revision_view, validate_formation,
)
from app.services.run_reliability_service import default_run_budget, deadline_from_budget
from app.services.cursor_service import SortKey, paginate_scalars
from app.services.group_collaboration_service import (
    GroupPolicyError,
    create_followup_group_run,
    create_group_with_initial_run,
    create_user_group_message,
    group_run_member_view,
    group_session_view,
    membership_view,
    native_group_run_view,
)

router = APIRouter(tags=["groups"])
NON_TERMINAL_WORK = {"queued", "running", "waiting_user", "waiting_agent", "waiting_tool", "waiting_resource"}


def expected(value: str | None) -> int:
    raw = (value or "").replace("W/", "").strip('" ')
    if not raw.isdigit(): raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    return int(raw)


async def owned_proposal(db: AsyncSession, proposal_id: str, actor: SessionActor) -> GroupFormationProposal:
    proposal = await db.get(GroupFormationProposal, proposal_id)
    if proposal is None: raise HTTPException(status_code=404, detail="Group Formation not found")
    await _owned_session(db, proposal.parent_session_id, actor)
    return proposal


async def owned_group(db: AsyncSession, group_id: str, actor: SessionActor, *, lock: bool = False) -> GroupSession:
    query = select(GroupSession).where(GroupSession.id == group_id)
    if lock: query = query.with_for_update()
    group = await db.scalar(query)
    if group is None: raise HTTPException(status_code=404, detail="Group not found")
    await _owned_session(db, group.parent_session_id, actor)
    return group


async def group_run_view(db: AsyncSession, group_run: GroupRun) -> dict:
    if group_run.owner_node_id is None:
        return await native_group_run_view(db, group_run)
    run = await db.get(Run, group_run.run_id)
    mappings = list(await db.scalars(select(GroupRunAgentVersion).where(
        GroupRunAgentVersion.group_run_id == group_run.id
    ).order_by(GroupRunAgentVersion.group_node_id)))
    started = await db.scalar(select(DurableRunner.id).where(
        DurableRunner.run_id == run.id, DurableRunner.status != "queued",
    ).limit(1))
    if run.lifecycle == "running" and started is None:
        display_phase = "created"
    elif run.lifecycle == "waiting":
        display_phase = run.wait_reason if run.wait_reason in {"waiting_user", "waiting_agent", "waiting_tool"} else "waiting"
    else:
        display_phase = run.lifecycle
    return {
        "id": group_run.id, "group_session_id": group_run.group_session_id,
        "owner_node_id": group_run.owner_node_id, "goal": group_run.goal,
        "display_phase": display_phase,
        "run": {"id": run.id, "lifecycle": run.lifecycle, "wait_reason": run.wait_reason,
                "outcome": run.outcome, "version": run.version,
                "result_summary": run.result_summary, "error_code": run.error_code},
        "agent_versions": [{
            "group_node_id": row.group_node_id,
            "participation_status": row.participation_status,
            "agent_version_id": row.agent_version_id,
            "unavailable_reason": row.unavailable_reason,
        } for row in mappings],
    }


async def _current_node_version(db: AsyncSession, node: GroupAgentNode) -> tuple[str | None, str | None]:
    agent = await db.get(AgentBlueprint, node.agent_id)
    version = await db.get(AgentAssemblyVersion, agent.current_published_version_id) if agent and agent.current_published_version_id else None
    if agent is None or agent.status != "published": return None, "agent_not_published"
    if version is None or version.immutable_at is None: return None, "published_version_unavailable"
    if not version.group_enabled: return None, "group_disabled"
    return version.id, None


def _group_runner(group: GroupSession, group_run: GroupRun, node: GroupAgentNode, version_id: str,
                  work: WorkItem, *, content: str) -> DurableRunner:
    return DurableRunner(
        id=uuid7(), work_item_id=work.id, run_id=work.run_id, session_id=work.session_id,
        agent_id=node.agent_id, agent_version_id=version_id, execution_epoch=work.execution_epoch,
        status="queued", principal_snapshot={"user_id": group.user_id, "session_id": group.parent_session_id,
            "entry_agent_id": group.entry_agent_id, "current_agent_id": node.agent_id, "mode": "group",
            "group_session_id": group.id, "group_run_id": group_run.id, "group_node_id": node.id,
            "delegation_chain": []}, assembly_snapshot={"agent_version_id": version_id}, prompt_manifest={},
        input_snapshot={"group_session_id": group.id, "group_run_id": group_run.id,
                        "group_node_id": node.id, "goal": content, "acceptance_criteria": "Respond to the user request"},
    )


@router.post("/sessions/{session_id}/group-formations", status_code=202)
async def create_group_formation(
    session_id: str, body: GroupFormationCreateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"), db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    session = await _owned_session(db, session_id, actor)
    record, replay = await _idempotency(db, actor=actor, operation="create_group_formation",
                                        key=idempotency_key, request={"session_id": session_id, **body.model_dump()})
    if replay and record.response_snapshot: return record.response_snapshot
    agent = await db.get(AgentBlueprint, session.entry_agent_id)
    version = await db.get(AgentAssemblyVersion, agent.current_published_version_id) if agent and agent.current_published_version_id else None
    if agent is None or agent.status != "published" or version is None or not version.group_enabled:
        raise HTTPException(status_code=409, detail={"code": "entry_agent_group_unavailable"})
    group = GroupSession(id=uuid7(), parent_session_id=session.id, user_id=session.user_id,
                         entry_agent_id=agent.id, status="forming")
    run_budget = default_run_budget("group")
    run = Run(id=uuid7(), session_id=session.id, user_id=session.user_id, mode="group",
              execution_kind="proposal_generation", lifecycle="running", entry_agent_id=agent.id,
              hard_budget_snapshot=run_budget,
              deadline_at=deadline_from_budget(run_budget, mode="group"), started_at=utcnow())
    proposal = GroupFormationProposal(id=uuid7(), parent_session_id=session.id, group_session_id=group.id,
                                      user_id=session.user_id, entry_agent_id=agent.id,
                                      generation_run_id=run.id, status="generating", current_revision_no=0)
    message = Message(id=uuid7(), session_id=session.id, user_id=session.user_id, scope_type="chat",
                      sender_type="user", sender_user_id=session.user_id, recipient_type="session", role="user",
                      content=body.goal.strip(), content_format="markdown", status="committed", run_id=run.id)
    work = WorkItem(id=uuid7(), run_id=run.id, session_id=session.id, user_id=session.user_id, mode="group",
                    owner_agent_id=agent.id, agent_version_id=version.id,
                    group_formation_proposal_id=proposal.id, goal=f"Generate Group Formation: {body.goal.strip()}",
                    status="queued", execution_epoch=1, attempt_no=1)
    runner = DurableRunner(id=uuid7(), work_item_id=work.id, run_id=run.id, session_id=session.id,
                           agent_id=agent.id, agent_version_id=version.id, execution_epoch=1, status="queued",
                           principal_snapshot={"user_id": session.user_id, "session_id": session.id,
                                               "entry_agent_id": agent.id, "current_agent_id": agent.id,
                                               "mode": "group", "delegation_chain": []},
                           assembly_snapshot={"agent_version_id": version.id}, prompt_manifest={},
                           input_snapshot={"group_formation_proposal_id": proposal.id, "goal": body.goal.strip()})
    message.work_item_id = work.id
    db.add_all([group, run]); await db.flush()
    db.add(proposal); await db.flush()
    await persist_new_work_runner(db, work, runner)
    db.add(message); await db.flush()
    events = ExecutionEventWriter(db)
    started = await events.append(session_id=session.id, run_id=run.id, event_type="group_formation.started",
        event_dedupe_key=f"command:group_formation:{record.id}:start", actor_type="user", actor_id=session.user_id,
        entity_type="group_formation_proposal", entity_id=proposal.id, entity_version=proposal.version,
        visibility="user", summary="Group Formation generation started", payload={"group_session_id": group.id},
        correlation_id=run.id)
    queued = await events.append(session_id=session.id, run_id=run.id, event_type="runner.queued",
        event_dedupe_key=f"command:group_formation:{record.id}:runner", actor_type="service", actor_id="group-api",
        entity_type="runner", entity_id=runner.id, entity_version=runner.version, visibility="private",
        private_owner=("runner", runner.id), summary="Group Formation Runner queued", payload={},
        correlation_id=run.id, caused_by_event_id=started.id)
    await OutboxWriter(db).enqueue(topic="runtime.runner", action="execute", aggregate_type="runner",
        aggregate_id=runner.id, partition_key=session.id, dedupe_key=f"event:{queued.id}:execute",
        payload_ref={"runner_id": runner.id, "group_formation_proposal_id": proposal.id})
    response = await proposal_view(db, proposal)
    record.resource_type, record.resource_id, record.http_status = "group_formation_proposal", proposal.id, 202
    record.response_snapshot = response; await db.commit(); return response


@router.get("/sessions/{session_id}/group-formations")
async def list_group_formations(
    session_id: str, limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    await _owned_session(db, session_id, actor)
    rows, next_cursor = await paginate_scalars(
        db, select(GroupFormationProposal).where(GroupFormationProposal.parent_session_id == session_id),
        sort=(SortKey(GroupFormationProposal.created_at), SortKey(GroupFormationProposal.id)),
        limit=limit, cursor=cursor, principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=f"session:{session_id}:group-formations", filters={},
    )
    return {"items": [await proposal_view(db, row) for row in rows], "next_cursor": next_cursor}


@router.get("/group-formations/{proposal_id}")
async def get_group_formation(proposal_id: str, db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor)):
    return await proposal_view(db, await owned_proposal(db, proposal_id, actor))


@router.get("/group-formations/{proposal_id}/revisions")
async def list_group_formation_revisions(
    proposal_id: str, limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    proposal = await owned_proposal(db, proposal_id, actor)
    rows, next_cursor = await paginate_scalars(
        db, select(GroupFormationRevision).where(GroupFormationRevision.proposal_id == proposal.id),
        sort=(SortKey(GroupFormationRevision.revision_no), SortKey(GroupFormationRevision.id)),
        limit=limit, cursor=cursor, principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=f"group-formation:{proposal_id}:revisions", filters={},
    )
    return {"items": [await revision_view(db, row) for row in rows], "next_cursor": next_cursor}


@router.get("/group-formations/{proposal_id}/revisions/{revision_no}")
async def get_group_formation_revision(
    proposal_id: str, revision_no: int, db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    proposal = await owned_proposal(db, proposal_id, actor)
    revision = await db.scalar(select(GroupFormationRevision).where(
        GroupFormationRevision.proposal_id == proposal.id,
        GroupFormationRevision.revision_no == revision_no,
    ))
    if revision is None: raise HTTPException(status_code=404, detail="Group Formation revision not found")
    return await revision_view(db, revision)


@router.patch("/group-formations/{proposal_id}")
async def patch_group_formation(
    proposal_id: str, body: GroupFormationPatchRequest,
    if_match: str | None = Header(default=None, alias="If-Match"), idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    record, replay = await _idempotency(db, actor=actor, operation="patch_group_formation", key=idempotency_key,
                                        request={"proposal_id": proposal_id, **body.model_dump(mode="json")})
    if replay and record.response_snapshot: return record.response_snapshot
    visible = await owned_proposal(db, proposal_id, actor)
    await db.scalar(select(UserSession).where(UserSession.id == visible.parent_session_id).with_for_update())
    proposal = await db.scalar(select(GroupFormationProposal).where(GroupFormationProposal.id == proposal_id).with_for_update())
    if proposal.version != expected(if_match) or proposal.current_revision_no != body.expected_revision_no:
        raise HTTPException(status_code=409, detail={"code": "group_revision_conflict", "version": proposal.version,
                                                    "current_revision_no": proposal.current_revision_no})
    if proposal.status != "awaiting_confirmation": raise HTTPException(status_code=409, detail={"code": "formation_not_editable"})
    try:
        revision = await create_revision(db, proposal, goal=body.goal, bootstrap_context=body.bootstrap_context,
            participants=body.participants, work_items=body.initial_work_items, edges=body.suggested_edges,
            source="user_edit", edited_by_type="user", edited_by_id=proposal.user_id)
    except GroupFormationValidationError as error:
        raise HTTPException(status_code=422, detail={"code": "invalid_group_formation", "issues": error.issues})
    await ExecutionEventWriter(db).append(session_id=proposal.parent_session_id, run_id=proposal.generation_run_id,
        event_type="group_formation.revised", event_dedupe_key=f"command:group_formation_revise:{record.id}",
        actor_type="user", actor_id=proposal.user_id, entity_type="group_formation_revision", entity_id=revision.id,
        visibility="user", summary="Group Formation revision saved", payload={"revision_no": revision.revision_no},
        correlation_id=proposal.generation_run_id)
    response = await proposal_view(db, proposal); record.resource_type = "group_formation_proposal"
    record.resource_id, record.http_status, record.response_snapshot = proposal.id, 200, response
    await db.commit(); return response


@router.post("/group-formations/{proposal_id}/confirm")
async def confirm_group_formation(
    proposal_id: str, body: GroupFormationConfirmRequest,
    if_match: str | None = Header(default=None, alias="If-Match"), idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    record, replay = await _idempotency(db, actor=actor, operation="confirm_group_formation", key=idempotency_key,
                                        request={"proposal_id": proposal_id, **body.model_dump()})
    if replay and record.response_snapshot: return record.response_snapshot
    visible = await owned_proposal(db, proposal_id, actor)
    await db.scalar(select(UserSession).where(UserSession.id == visible.parent_session_id).with_for_update())
    generation_run = await db.scalar(select(Run).where(Run.id == visible.generation_run_id).with_for_update())
    proposal = await db.scalar(select(GroupFormationProposal).where(GroupFormationProposal.id == proposal_id).with_for_update())
    group = await db.scalar(select(GroupSession).where(GroupSession.id == proposal.group_session_id).with_for_update())
    if proposal.version != expected(if_match) or proposal.current_revision_no != body.expected_revision_no:
        raise HTTPException(status_code=409, detail={"code": "group_revision_conflict", "version": proposal.version,
                                                    "current_revision_no": proposal.current_revision_no})
    if proposal.status != "awaiting_confirmation" or generation_run.lifecycle != "completed" or group.status != "forming":
        raise HTTPException(status_code=409, detail={"code": "formation_not_confirmable"})
    revision = await db.scalar(select(GroupFormationRevision).where(
        GroupFormationRevision.id == proposal.current_revision_id).with_for_update())
    participants = list(await db.scalars(select(GroupFormationParticipant).where(
        GroupFormationParticipant.revision_id == revision.id).order_by(GroupFormationParticipant.sort_order)))
    by_participant_id = {row.id: row for row in participants}
    work_defs = list(await db.scalars(select(GroupFormationWorkItem).where(
        GroupFormationWorkItem.revision_id == revision.id).order_by(GroupFormationWorkItem.sort_order)))
    edge_defs = list(await db.scalars(select(GroupFormationSuggestedEdge).where(
        GroupFormationSuggestedEdge.revision_id == revision.id).order_by(GroupFormationSuggestedEdge.sort_order)))
    participant_inputs = [GroupParticipantInput(participant_key=row.participant_key, agent_id=row.agent_id,
        is_entry=row.is_entry, initial_responsibility=row.initial_responsibility) for row in participants]
    work_inputs = [GroupInitialWorkItemInput(work_item_key=row.work_item_key,
        participant_key=by_participant_id[row.participant_id].participant_key, title=row.title, goal=row.goal,
        acceptance_criteria=row.acceptance_criteria) for row in work_defs]
    edge_inputs = [GroupSuggestedEdgeInput(source_participant_key=by_participant_id[row.source_participant_id].participant_key,
        target_participant_key=by_participant_id[row.target_participant_id].participant_key, label=row.label,
        rationale=row.rationale) for row in edge_defs]
    try: versions = await validate_formation(db, proposal, participant_inputs, work_inputs, edge_inputs, resolve_versions=True)
    except GroupFormationValidationError as error: raise HTTPException(status_code=422, detail={"code": "invalid_group_formation", "issues": error.issues})
    group.frozen_formation_revision_id = revision.id; group.goal = revision.goal
    group.bootstrap_context = revision.bootstrap_context; group.status = "open"; group.confirmed_at = utcnow(); group.version += 1
    nodes: dict[str, GroupAgentNode] = {}
    for participant in participants:
        node = GroupAgentNode(group_session_id=group.id, agent_id=participant.agent_id,
            source_participant_id=participant.id, is_entry=participant.is_entry,
            initial_responsibility=participant.initial_responsibility, availability="available")
        db.add(node); nodes[participant.id] = node
    await db.flush()
    for edge in edge_defs:
        db.add(GroupInitialSuggestedEdge(group_session_id=group.id, source_node_id=nodes[edge.source_participant_id].id,
            target_node_id=nodes[edge.target_participant_id].id, label=edge.label, rationale=edge.rationale,
            source_edge_id=edge.id))
    entry_participant = next(row for row in participants if row.is_entry); owner_node = nodes[entry_participant.id]
    run_budget = default_run_budget("group")
    run = Run(id=uuid7(), session_id=group.parent_session_id, user_id=group.user_id, mode="group",
              execution_kind="normal", lifecycle="running", entry_agent_id=group.entry_agent_id,
              hard_budget_snapshot=run_budget,
              deadline_at=deadline_from_budget(run_budget, mode="group"), started_at=utcnow())
    group_run = GroupRun(id=uuid7(), run_id=run.id, group_session_id=group.id, owner_node_id=owner_node.id,
                         goal=revision.goal)
    db.add(run); await db.flush()
    db.add(group_run); await db.flush()
    for participant in participants:
        node = nodes[participant.id]
        db.add(GroupRunAgentVersion(group_run_id=group_run.id, group_session_id=group.id, group_node_id=node.id,
                                    agent_version_id=versions[participant.agent_id], participation_status="available"))
    events = ExecutionEventWriter(db)
    confirmed = await events.append(session_id=group.parent_session_id, run_id=run.id,
        event_type="group_formation.confirmed", event_dedupe_key=f"command:group_formation_confirm:{record.id}",
        actor_type="user", actor_id=group.user_id, entity_type="group_session", entity_id=group.id,
        entity_version=group.version, visibility="user", summary="Group Formation confirmed",
        payload={"revision_no": revision.revision_no, "group_run_id": group_run.id}, correlation_id=run.id)
    for definition in work_defs:
        participant = by_participant_id[definition.participant_id]; node = nodes[participant.id]
        work = WorkItem(id=uuid7(), run_id=run.id, session_id=group.parent_session_id, user_id=group.user_id,
            mode="group", owner_agent_id=participant.agent_id, agent_version_id=versions[participant.agent_id],
            group_formation_work_item_id=definition.id, group_run_id=group_run.id, group_node_id=node.id,
            goal=definition.goal, status="queued", execution_epoch=1, attempt_no=1)
        runner = DurableRunner(id=uuid7(), work_item_id=work.id, run_id=run.id, session_id=group.parent_session_id,
            agent_id=participant.agent_id, agent_version_id=versions[participant.agent_id], execution_epoch=1,
            status="queued", principal_snapshot={"user_id": group.user_id, "session_id": group.parent_session_id,
                "entry_agent_id": group.entry_agent_id, "current_agent_id": participant.agent_id, "mode": "group",
                "group_session_id": group.id, "group_run_id": group_run.id, "group_node_id": node.id,
                "delegation_chain": []}, assembly_snapshot={"agent_version_id": versions[participant.agent_id]},
            prompt_manifest={}, input_snapshot={"group_session_id": group.id, "group_run_id": group_run.id,
                "group_node_id": node.id, "group_formation_work_item_id": definition.id,
                "goal": definition.goal, "acceptance_criteria": definition.acceptance_criteria})
        await persist_new_work_runner(db, work, runner)
        queued = await events.append(session_id=group.parent_session_id, run_id=run.id, event_type="runner.queued",
            event_dedupe_key=f"transition:runner:{runner.id}:0:1:group_initial", actor_type="service",
            actor_id="group-runtime", entity_type="runner", entity_id=runner.id, entity_version=runner.version,
            visibility="private", private_owner=("runner", runner.id), summary="Group node Runner queued",
            payload={"group_node_id": node.id}, correlation_id=run.id, caused_by_event_id=confirmed.id)
        await OutboxWriter(db).enqueue(topic="runtime.runner", action="execute", aggregate_type="runner",
            aggregate_id=runner.id, partition_key=group.parent_session_id, dedupe_key=f"event:{queued.id}:execute",
            payload_ref={"runner_id": runner.id, "group_run_id": group_run.id})
    proposal.status = "confirmed"; proposal.decided_at = utcnow(); proposal.version += 1
    response = {"proposal": await proposal_view(db, proposal), "group_session": {"id": group.id, "status": group.status,
        "version": group.version}, "group_run": {"id": group_run.id, "run_id": run.id, "owner_node_id": owner_node.id},
        "run": {"id": run.id, "lifecycle": run.lifecycle, "outcome": run.outcome, "version": run.version}}
    record.resource_type, record.resource_id, record.http_status = "group_formation_proposal", proposal.id, 200
    record.response_snapshot = response; await db.commit(); return response


async def _decline_formation(
    proposal_id: str, decision: str, if_match: str | None, idempotency_key: str,
    db: AsyncSession, actor: SessionActor,
):
    record, replay = await _idempotency(db, actor=actor, operation=f"{decision}_group_formation",
                                        key=idempotency_key, request={"proposal_id": proposal_id})
    if replay and record.response_snapshot: return record.response_snapshot
    visible = await owned_proposal(db, proposal_id, actor)
    await db.scalar(select(UserSession).where(UserSession.id == visible.parent_session_id).with_for_update())
    run = await db.scalar(select(Run).where(Run.id == visible.generation_run_id).with_for_update())
    proposal = await db.scalar(select(GroupFormationProposal).where(
        GroupFormationProposal.id == proposal_id).with_for_update())
    group = await db.scalar(select(GroupSession).where(GroupSession.id == proposal.group_session_id).with_for_update())
    if proposal.version != expected(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "version": proposal.version})
    if proposal.status not in {"generating", "awaiting_confirmation"}:
        raise HTTPException(status_code=409, detail={"code": "formation_terminal", "status": proposal.status})
    proposal.status = decision; proposal.decided_at = utcnow(); proposal.version += 1
    group.status = "closed"; group.closed_at = utcnow(); group.version += 1
    if run.lifecycle not in {"completed", "cancelled"}:
        works = list(await db.scalars(select(WorkItem).where(WorkItem.run_id == run.id).with_for_update()))
        for work in works:
            if work.status in {"queued", "running", "waiting_user", "waiting_agent", "waiting_tool", "waiting_resource"}:
                runners = list(await db.scalars(select(DurableRunner).where(
                    DurableRunner.work_item_id == work.id,
                    DurableRunner.status.in_(("queued", "running")),
                ).with_for_update()))
                for runner in runners: runner.status = "cancelled"; runner.lease_expires_at = None; runner.version += 1
                work.status = "cancelled"; work.active_runner_id = None; work.version += 1
        run.lifecycle = "cancelled"; run.wait_reason = None; run.outcome = None
        run.cancellation_requested_at = utcnow(); run.finished_at = utcnow(); run.version += 1
    await ExecutionEventWriter(db).append(session_id=proposal.parent_session_id, run_id=run.id,
        event_type=f"group_formation.{decision}", event_dedupe_key=f"command:group_formation_{decision}:{record.id}",
        actor_type="user", actor_id=proposal.user_id, entity_type="group_formation_proposal",
        entity_id=proposal.id, entity_version=proposal.version, visibility="user",
        summary=f"Group Formation {decision}", payload={"group_session_id": group.id}, correlation_id=run.id)
    response = await proposal_view(db, proposal); record.resource_type = "group_formation_proposal"
    record.resource_id, record.http_status, record.response_snapshot = proposal.id, 200, response
    await db.commit(); return response


@router.post("/group-formations/{proposal_id}/reject")
async def reject_group_formation(
    proposal_id: str, if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"), db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    return await _decline_formation(proposal_id, "rejected", if_match, idempotency_key, db, actor)


@router.post("/group-formations/{proposal_id}/cancel")
async def cancel_group_formation(
    proposal_id: str, if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"), db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    return await _decline_formation(proposal_id, "cancelled", if_match, idempotency_key, db, actor)


def _raise_group_policy(error: GroupPolicyError) -> None:
    status = 404 if error.code == "agent_not_discoverable" else 422
    raise HTTPException(
        status_code=status,
        detail={"code": error.code, "message": str(error), **error.details},
    )


@router.post("/sessions/{session_id}/groups", status_code=201)
async def create_group(
    session_id: str,
    body: GroupCreateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    """Create a Group directly from user-selected Agent Network identities."""
    session = await _owned_session(db, session_id, actor)
    if actor.account is None:
        raise HTTPException(status_code=403, detail={"code": "identity_mapping_required"})
    request = {
        "session_id": session_id,
        "member_agent_ids": body.member_agent_ids,
        "goal": body.goal.strip(),
        "reporter_agent_id": body.reporter_agent_id,
        "shared_context_refs": sorted(set(body.shared_context_refs)),
    }
    record, replay = await _idempotency(
        db, actor=actor, operation="create_agent_native_group",
        key=idempotency_key, request=request,
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    try:
        group, group_run = await create_group_with_initial_run(
            db,
            session=session,
            account_id=actor.account.id,
            member_agent_ids=body.member_agent_ids,
            goal=body.goal.strip(),
            reporter_agent_id=body.reporter_agent_id,
            shared_context_refs=list(dict.fromkeys(body.shared_context_refs)),
            event_key=f"create_group:{record.id}",
        )
    except GroupPolicyError as error:
        await db.rollback()
        _raise_group_policy(error)
    response = group_session_view(group)
    # The GroupSession wire contract remains small; the first GroupRun is
    # discoverable through GET /groups/{id}/runs.
    record.resource_type = "group_session"
    record.resource_id = group.id
    record.http_status = 201
    record.response_snapshot = response
    await db.commit()
    return response


@router.get("/sessions/{session_id}/groups")
async def list_session_groups(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    session = await _owned_session(db, session_id, actor)
    rows = list(await db.scalars(select(GroupSession).where(
        GroupSession.parent_session_id == session.id,
        GroupSession.status.in_(("open", "closed", "archived")),
    ).order_by(GroupSession.created_at.desc(), GroupSession.id.desc())))
    return {"items": [group_session_view(item) for item in rows], "next_cursor": None}


@router.get("/groups/{group_id}")
async def get_group(group_id: str, db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor)):
    group = await owned_group(db, group_id, actor)
    legacy_node = await db.scalar(select(GroupAgentNode.id).where(
        GroupAgentNode.group_session_id == group.id,
    ).limit(1))
    if legacy_node is None:
        return group_session_view(group)
    return {"id": group.id, "parent_session_id": group.parent_session_id, "entry_agent_id": group.entry_agent_id,
            "status": group.status, "goal": group.goal, "bootstrap_context": group.bootstrap_context,
            "frozen_formation_revision_id": group.frozen_formation_revision_id, "version": group.version}


@router.get("/groups/{group_id}/memberships")
async def list_group_memberships(
    group_id: str,
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    group = await owned_group(db, group_id, actor)
    rows = list(await db.scalars(select(GroupMembership).where(
        GroupMembership.group_session_id == group.id
    ).order_by(GroupMembership.created_at, GroupMembership.id)))
    return {"items": [membership_view(item) for item in rows], "next_cursor": None}


@router.get("/groups/{group_id}/nodes")
async def list_group_nodes(
    group_id: str, limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    group = await owned_group(db, group_id, actor)
    rows, next_cursor = await paginate_scalars(
        db, select(GroupAgentNode).where(GroupAgentNode.group_session_id == group.id),
        sort=(SortKey(GroupAgentNode.created_at, descending=False), SortKey(GroupAgentNode.id, descending=False)),
        limit=limit, cursor=cursor, principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=f"group:{group_id}:nodes", filters={},
    )
    items = []
    for row in rows:
        version_id, reason = await _current_node_version(db, row)
        items.append({"id": row.id, "agent_id": row.agent_id, "is_entry": row.is_entry,
                      "initial_responsibility": row.initial_responsibility,
                      "availability": "available" if version_id else "unavailable",
                      "unavailable_reason": reason, "current_agent_version_id": version_id,
                      "version": row.version})
    return {"items": items, "next_cursor": next_cursor}


@router.get("/groups/{group_id}/suggested-edges")
async def list_group_suggested_edges(
    group_id: str, limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    group = await owned_group(db, group_id, actor)
    rows, next_cursor = await paginate_scalars(
        db, select(GroupInitialSuggestedEdge).where(GroupInitialSuggestedEdge.group_session_id == group.id),
        sort=(SortKey(GroupInitialSuggestedEdge.source_node_id, descending=False),
              SortKey(GroupInitialSuggestedEdge.target_node_id, descending=False)),
        limit=limit, cursor=cursor, principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=f"group:{group_id}:suggested-edges", filters={},
    )
    return {"items": [{"source_node_id": row.source_node_id, "target_node_id": row.target_node_id,
                       "label": row.label, "rationale": row.rationale} for row in rows],
            "next_cursor": next_cursor}


@router.get("/groups/{group_id}/collaboration-edges")
async def list_group_collaboration_edges(
    group_id: str, limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    group = await owned_group(db, group_id, actor)
    rows, next_cursor = await paginate_scalars(
        db, select(GroupCollaborationEdge).where(GroupCollaborationEdge.group_session_id == group.id),
        sort=(SortKey(GroupCollaborationEdge.created_at, descending=False),
              SortKey(GroupCollaborationEdge.id, descending=False)),
        limit=limit, cursor=cursor, principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=f"group:{group_id}:collaboration-edges", filters={},
    )
    return {"items": [{"id": row.id, "source_node_id": row.source_node_id,
                       "target_node_id": row.target_node_id, "first_invocation_id": row.first_invocation_id,
                       "created_in_group_run_id": row.created_in_group_run_id,
                       "created_at": row.created_at.isoformat()} for row in rows],
            "next_cursor": next_cursor}


@router.get("/groups/{group_id}/timeline")
async def get_group_timeline(
    group_id: str, after_sequence: int = 0, limit: int = 100,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    group = await owned_group(db, group_id, actor)
    run_ids = select(GroupRun.run_id).where(GroupRun.group_session_id == group.id)
    rows = list(await db.scalars(select(ExecutionEvent).where(
        ExecutionEvent.session_id == group.parent_session_id,
        ExecutionEvent.sequence > after_sequence,
        (ExecutionEvent.group_session_id == group.id) | ExecutionEvent.run_id.in_(run_ids),
        ExecutionEvent.visibility.in_(("user", "group", "direct")),
    ).order_by(ExecutionEvent.sequence).limit(max(1, min(limit, 100)))))
    return {"items": [{"event_id": row.id, "sequence": row.sequence, "run_id": row.run_id,
                       "event_type": row.event_type, "summary": row.summary, "payload": row.payload,
                       "caused_by_event_id": row.caused_by_event_id,
                       "created_at": row.created_at.isoformat()} for row in rows],
            "next_cursor": rows[-1].sequence if rows else None}


@router.get("/groups/{group_id}/runs")
async def list_group_runs(
    group_id: str, limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    group = await owned_group(db, group_id, actor)
    rows, next_cursor = await paginate_scalars(
        db, select(GroupRun).where(GroupRun.group_session_id == group.id),
        sort=(SortKey(GroupRun.created_at), SortKey(GroupRun.id)),
        limit=limit, cursor=cursor, principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=f"group:{group_id}:runs", filters={},
    )
    return {"items": [await group_run_view(db, row) for row in rows], "next_cursor": next_cursor}


@router.get("/group-runs/{group_run_id}")
async def get_group_run(
    group_run_id: str, db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    group_run = await db.get(GroupRun, group_run_id)
    if group_run is None: raise HTTPException(status_code=404, detail="GroupRun not found")
    await owned_group(db, group_run.group_session_id, actor)
    return await group_run_view(db, group_run)


@router.get("/group-runs/{group_run_id}/members")
async def list_group_run_members(
    group_run_id: str,
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    group_run = await db.get(GroupRun, group_run_id)
    if group_run is None:
        raise HTTPException(status_code=404, detail="GroupRun not found")
    await owned_group(db, group_run.group_session_id, actor)
    rows = list(await db.scalars(select(GroupRunMember).where(
        GroupRunMember.group_run_id == group_run.id
    ).order_by(GroupRunMember.agent_id)))
    return {"items": [group_run_member_view(item) for item in rows], "next_cursor": None}


@router.get("/group-runs/{group_run_id}/interaction-graph")
async def get_group_interaction_graph(
    group_run_id: str,
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    """Project only real directed Message and AgentInvocation facts."""
    group_run = await db.get(GroupRun, group_run_id)
    if group_run is None:
        raise HTTPException(status_code=404, detail="GroupRun not found")
    await owned_group(db, group_run.group_session_id, actor)
    members = list(await db.scalars(select(GroupRunMember).where(
        GroupRunMember.group_run_id == group_run.id
    ).order_by(GroupRunMember.agent_id)))
    member_ids = {item.agent_id for item in members}
    messages = list(await db.scalars(select(Message).where(
        Message.group_run_id == group_run.id,
        Message.sender_agent_id.is_not(None),
        Message.recipient_agent_id.is_not(None),
        Message.status == "committed",
    ).order_by(Message.created_at, Message.id)))
    invocations = list(await db.scalars(select(AgentInvocation).where(
        AgentInvocation.group_run_id == group_run.id,
    ).order_by(AgentInvocation.created_at, AgentInvocation.id)))
    edges = [{
        "source_agent_id": item.sender_agent_id,
        "target_agent_id": item.recipient_agent_id,
        "cause_type": "message",
        "cause_id": item.id,
        "status": "committed",
        "created_at": item.created_at.isoformat(),
    } for item in messages if item.sender_agent_id in member_ids and item.recipient_agent_id in member_ids]
    edges.extend({
        "source_agent_id": item.caller_agent_id,
        "target_agent_id": item.target_agent_id,
        "cause_type": "agent_invocation",
        "cause_id": item.id,
        "status": item.status,
        "created_at": item.created_at.isoformat(),
    } for item in invocations if item.caller_agent_id in member_ids and item.target_agent_id in member_ids)
    edges.sort(key=lambda item: (item["created_at"], item["cause_id"]))
    return {"nodes": [group_run_member_view(item) for item in members], "edges": edges}


@router.post("/groups/{group_id}/runs", status_code=202)
async def create_group_run(
    group_id: str, body: GroupRunCreateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    content = body.content.strip()
    record, replay = await _idempotency(
        db, actor=actor, operation="create_group_run", key=idempotency_key,
        request={"group_id": group_id, "content": content,
                 "recipient_node_id": body.recipient_node_id,
                 "recipient_agent_id": body.recipient_agent_id,
                 "reporter_agent_id": body.reporter_agent_id,
                 "reference_run_ids": sorted(set(body.reference_run_ids))},
    )
    if replay and record.response_snapshot: return record.response_snapshot
    visible = await owned_group(db, group_id, actor)
    legacy_node = await db.scalar(select(GroupAgentNode.id).where(
        GroupAgentNode.group_session_id == visible.id,
    ).limit(1))
    if legacy_node is None:
        if actor.account is None:
            raise HTTPException(status_code=403, detail={"code": "identity_mapping_required"})
        session = await _owned_session(db, visible.parent_session_id, actor)
        try:
            group_run = await create_followup_group_run(
                db, session=session, group=visible, account_id=actor.account.id,
                content=content,
                recipient_agent_id=body.recipient_agent_id,
                reporter_agent_id=body.reporter_agent_id,
                reference_run_ids=list(dict.fromkeys(body.reference_run_ids)),
                event_key=f"create_group_run:{record.id}",
            )
        except GroupPolicyError as error:
            await db.rollback()
            _raise_group_policy(error)
        response = await native_group_run_view(db, group_run)
        record.resource_type = "group_run"
        record.resource_id = group_run.id
        record.http_status = 202
        record.response_snapshot = response
        await db.commit()
        return response
    await db.scalar(select(UserSession).where(UserSession.id == visible.parent_session_id).with_for_update())
    group = await db.scalar(select(GroupSession).where(GroupSession.id == group_id).with_for_update())
    if group.status != "open": raise HTTPException(status_code=409, detail={"code": "group_not_open"})
    nodes = list(await db.scalars(select(GroupAgentNode).where(
        GroupAgentNode.group_session_id == group.id
    ).order_by(GroupAgentNode.created_at, GroupAgentNode.id).with_for_update()))
    owner = next((node for node in nodes if node.id == body.recipient_node_id), None) if body.recipient_node_id else next(
        (node for node in nodes if node.is_entry), None
    )
    if owner is None: raise HTTPException(status_code=422, detail={"code": "group_node_not_found"})
    frozen: dict[str, tuple[str | None, str | None]] = {}
    available_node_ids = []
    for node in nodes:
        version_id, reason = await _current_node_version(db, node)
        frozen[node.id] = (version_id, reason)
        availability = "available" if version_id else "unavailable"
        if node.availability != availability or node.unavailable_reason != reason:
            node.availability = availability; node.unavailable_reason = reason; node.version += 1
        if version_id: available_node_ids.append(node.id)
    owner_version, owner_reason = frozen[owner.id]
    if owner_version is None:
        raise HTTPException(status_code=409, detail={"code": "group_owner_unavailable",
            "owner_node_id": owner.id, "unavailable_reason": owner_reason,
            "available_node_ids": available_node_ids})
    reference_ids = list(dict.fromkeys(body.reference_run_ids))
    references = list(await db.scalars(select(GroupRun).where(
        GroupRun.id.in_(reference_ids), GroupRun.group_session_id == group.id
    ))) if reference_ids else []
    if len(references) != len(reference_ids):
        raise HTTPException(status_code=422, detail={"code": "invalid_group_run_reference"})
    referenced_runs = list(await db.scalars(select(Run).where(
        Run.id.in_([item.run_id for item in references])
    ))) if references else []
    if any(run.lifecycle not in {"completed", "cancelled"} for run in referenced_runs):
        raise HTTPException(status_code=409, detail={"code": "group_run_reference_not_terminal"})

    run_budget = default_run_budget("group")
    run = Run(id=uuid7(), session_id=group.parent_session_id, user_id=group.user_id, mode="group",
              execution_kind="normal", lifecycle="running", entry_agent_id=group.entry_agent_id,
              hard_budget_snapshot=run_budget,
              deadline_at=deadline_from_budget(run_budget, mode="group"), started_at=utcnow())
    group_run = GroupRun(id=uuid7(), run_id=run.id, group_session_id=group.id,
                         owner_node_id=owner.id, goal=content)
    message = Message(id=uuid7(), session_id=group.parent_session_id, user_id=group.user_id,
        scope_type="group", group_session_id=group.id, group_run_id=group_run.id,
        sender_type="user", sender_user_id=group.user_id, recipient_type="group_node",
        recipient_group_node_id=owner.id, role="user", content=content,
        content_format="markdown", status="committed", run_id=run.id)
    work = WorkItem(id=uuid7(), run_id=run.id, session_id=group.parent_session_id,
        user_id=group.user_id, mode="group", owner_agent_id=owner.agent_id,
        agent_version_id=owner_version, group_run_id=group_run.id, group_node_id=owner.id,
        goal=content, status="queued", execution_epoch=1, attempt_no=1)
    runner = _group_runner(group, group_run, owner, owner_version, work, content=content)
    message.work_item_id = work.id
    db.add(run); await db.flush()
    db.add(group_run); await db.flush()
    await persist_new_work_runner(db, work, runner)
    db.add(message); await db.flush()
    for node in nodes:
        version_id, reason = frozen[node.id]
        db.add(GroupRunAgentVersion(group_run_id=group_run.id, group_session_id=group.id,
            group_node_id=node.id, agent_version_id=version_id,
            participation_status="available" if version_id else "unavailable",
            unavailable_reason=reason))
    for reference in references:
        db.add(GroupRunReference(group_run_id=group_run.id, referenced_group_run_id=reference.id))
    events = ExecutionEventWriter(db)
    created = await events.append(session_id=group.parent_session_id, run_id=run.id,
        group_session_id=group.id, group_run_id=group_run.id, event_type="group_run.created",
        event_dedupe_key=f"command:create_group_run:{record.id}", actor_type="user", actor_id=group.user_id,
        entity_type="group_run", entity_id=group_run.id, entity_version=group_run.version,
        visibility="group", summary="GroupRun created",
        payload={"owner_node_id": owner.id, "reference_run_ids": reference_ids}, correlation_id=run.id)
    queued = await events.append(session_id=group.parent_session_id, run_id=run.id,
        group_session_id=group.id, group_run_id=group_run.id, event_type="runner.queued",
        event_dedupe_key=f"transition:runner:{runner.id}:0:1:group_run", actor_type="service",
        actor_id="group-runtime", entity_type="runner", entity_id=runner.id,
        entity_version=runner.version, visibility="private", private_owner=("runner", runner.id),
        summary="GroupRun owner Runner queued", payload={"group_node_id": owner.id},
        correlation_id=run.id, caused_by_event_id=created.id)
    await OutboxWriter(db).enqueue(topic="runtime.runner", action="execute", aggregate_type="runner",
        aggregate_id=runner.id, partition_key=group.parent_session_id,
        dedupe_key=f"event:{queued.id}:execute", payload_ref={"runner_id": runner.id, "group_run_id": group_run.id})
    response = await group_run_view(db, group_run)
    record.resource_type, record.resource_id, record.http_status = "group_run", group_run.id, 202
    record.response_snapshot = response; await db.commit(); return response


@router.post("/group-runs/{group_run_id}/messages", status_code=202)
async def create_group_run_message(
    group_run_id: str, body: GroupRunMessageRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    content = body.content.strip()
    record, replay = await _idempotency(db, actor=actor, operation="create_group_run_message",
        key=idempotency_key, request={"group_run_id": group_run_id, "content": content,
                                      "recipient_node_id": body.recipient_node_id,
                                      "recipient_agent_id": body.recipient_agent_id})
    if replay and record.response_snapshot: return record.response_snapshot
    visible = await db.get(GroupRun, group_run_id)
    if visible is None: raise HTTPException(status_code=404, detail="GroupRun not found")
    group = await owned_group(db, visible.group_session_id, actor)
    if visible.owner_node_id is None:
        session = await _owned_session(db, group.parent_session_id, actor)
        try:
            message, _runner = await create_user_group_message(
                db, session=session, group=group, group_run=visible,
                content=content, recipient_agent_id=body.recipient_agent_id,
                event_key=f"group_run_message:{record.id}",
            )
        except GroupPolicyError as error:
            await db.rollback()
            _raise_group_policy(error)
        response = {
            "message": {
                "id": message.id,
                "recipient_agent_id": message.recipient_agent_id,
                "visibility": message.visibility,
            },
            "group_run": await native_group_run_view(db, visible),
        }
        record.resource_type = "message"
        record.resource_id = message.id
        record.http_status = 202
        record.response_snapshot = response
        await db.commit()
        return response
    await db.scalar(select(UserSession).where(UserSession.id == group.parent_session_id).with_for_update())
    group_run = await db.scalar(select(GroupRun).where(GroupRun.id == group_run_id).with_for_update())
    run = await db.scalar(select(Run).where(Run.id == group_run.run_id).with_for_update())
    if run.lifecycle in {"completed", "cancelled", "settling"}:
        raise HTTPException(status_code=409, detail={"code": "group_run_not_accepting_messages",
                                                    "lifecycle": run.lifecycle})
    if body.recipient_node_id:
        node = await db.scalar(select(GroupAgentNode).where(
            GroupAgentNode.id == body.recipient_node_id,
            GroupAgentNode.group_session_id == group.id,
        ).with_for_update())
    else:
        node = await db.scalar(select(GroupAgentNode).where(
            GroupAgentNode.group_session_id == group.id, GroupAgentNode.is_entry.is_(True),
        ).with_for_update())
    if node is None: raise HTTPException(status_code=422, detail={"code": "group_node_not_found"})
    mapping = await db.scalar(select(GroupRunAgentVersion).where(
        GroupRunAgentVersion.group_run_id == group_run.id,
        GroupRunAgentVersion.group_node_id == node.id,
    ))
    if mapping is None or mapping.participation_status != "available" or mapping.agent_version_id is None:
        raise HTTPException(status_code=409, detail={"code": "group_node_unavailable", "group_node_id": node.id})
    work = WorkItem(id=uuid7(), run_id=run.id, session_id=run.session_id, user_id=run.user_id,
        mode="group", owner_agent_id=node.agent_id, agent_version_id=mapping.agent_version_id,
        group_run_id=group_run.id, group_node_id=node.id, goal=content,
        status="queued", execution_epoch=1, attempt_no=1)
    runner = _group_runner(group, group_run, node, mapping.agent_version_id, work, content=content)
    message = Message(id=uuid7(), session_id=run.session_id, user_id=run.user_id, scope_type="group",
        group_session_id=group.id, group_run_id=group_run.id, sender_type="user",
        sender_user_id=run.user_id, recipient_type="group_node", recipient_group_node_id=node.id,
        role="user", content=content, content_format="markdown", status="committed",
        run_id=run.id, work_item_id=work.id)
    run.lifecycle = "running"; run.wait_reason = None; run.version += 1
    await persist_new_work_runner(db, work, runner)
    db.add(message); await db.flush()
    created = await ExecutionEventWriter(db).append(session_id=run.session_id, run_id=run.id,
        group_session_id=group.id, group_run_id=group_run.id, event_type="message.created",
        event_dedupe_key=f"command:group_run_message:{record.id}", actor_type="user", actor_id=run.user_id,
        entity_type="message", entity_id=message.id, visibility="group", summary="Group user message created",
        payload={"message_id": message.id, "recipient_node_id": node.id}, correlation_id=run.id)
    queued = await ExecutionEventWriter(db).append(session_id=run.session_id, run_id=run.id,
        group_session_id=group.id, group_run_id=group_run.id, event_type="runner.queued",
        event_dedupe_key=f"transition:runner:{runner.id}:0:1:group_message", actor_type="service",
        actor_id="group-runtime", entity_type="runner", entity_id=runner.id, entity_version=runner.version,
        visibility="private", private_owner=("runner", runner.id), summary="Directed Group Runner queued",
        payload={"group_node_id": node.id}, correlation_id=run.id, caused_by_event_id=created.id)
    await OutboxWriter(db).enqueue(topic="runtime.runner", action="execute", aggregate_type="runner",
        aggregate_id=runner.id, partition_key=run.session_id, dedupe_key=f"event:{queued.id}:execute",
        payload_ref={"runner_id": runner.id, "group_run_id": group_run.id})
    response = {"message": {"id": message.id, "recipient_node_id": node.id},
                "group_run": await group_run_view(db, group_run)}
    record.resource_type, record.resource_id, record.http_status = "message", message.id, 202
    record.response_snapshot = response; await db.commit(); return response


@router.get("/group-runs/{group_run_id}/messages")
async def list_group_run_messages(
    group_run_id: str, limit: int = 100, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    group_run = await db.get(GroupRun, group_run_id)
    if group_run is None: raise HTTPException(status_code=404, detail="GroupRun not found")
    await owned_group(db, group_run.group_session_id, actor)
    rows, next_cursor = await paginate_scalars(db, select(Message).where(
        Message.group_run_id == group_run.id, Message.scope_type == "group",
        Message.status == "committed",
    ), sort=(SortKey(Message.created_at, descending=False), SortKey(Message.id, descending=False)),
        limit=limit, cursor=cursor, principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=f"group-run:{group_run_id}:messages", filters={})
    return {"items": [{"id": row.id, "sender_type": row.sender_type,
                       "sender_user_id": row.sender_user_id, "sender_agent_id": row.sender_agent_id,
                       "recipient_type": row.recipient_type,
                       "recipient_group_node_id": row.recipient_group_node_id,
                       "recipient_agent_id": row.recipient_agent_id,
                       "visibility": row.visibility,
                       "role": row.role, "content": row.content,
                       "work_item_id": row.work_item_id, "clarification_id": row.clarification_id,
                       "created_at": row.created_at.isoformat()} for row in rows],
            "next_cursor": next_cursor}


@router.get("/groups/{group_id}/messages")
async def list_group_messages(
    group_id: str, limit: int = 100, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    group = await owned_group(db, group_id, actor)
    rows, next_cursor = await paginate_scalars(
        db,
        select(Message).where(
            Message.group_session_id == group.id,
            Message.scope_type == "group",
            Message.status == "committed",
            Message.visibility.in_(("group", "direct", "user")),
        ),
        sort=(SortKey(Message.created_at, descending=False), SortKey(Message.id, descending=False)),
        limit=limit, cursor=cursor,
        principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=f"group:{group_id}:messages", filters={},
    )
    return {"items": [{
        "id": row.id,
        "session_id": row.session_id,
        "group_run_id": row.group_run_id,
        "sender_kind": "runtime" if row.sender_type == "system" else row.sender_type,
        "sender_agent_id": row.sender_agent_id,
        "recipient_agent_id": row.recipient_agent_id,
        "visibility": row.visibility,
        "content": row.content,
        "run_id": row.run_id,
        "work_item_id": row.work_item_id,
        "sequence_no": row.sequence_no,
        "created_at": row.created_at.isoformat(),
    } for row in rows], "next_cursor": next_cursor}


@router.post("/group-runs/{group_run_id}/reporter", status_code=202)
async def replace_group_reporter(
    group_run_id: str,
    body: GroupReporterReplaceRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    record, replay = await _idempotency(
        db, actor=actor, operation="replace_group_reporter", key=idempotency_key,
        request={"group_run_id": group_run_id, **body.model_dump()},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    visible = await db.get(GroupRun, group_run_id)
    if visible is None:
        raise HTTPException(status_code=404, detail="GroupRun not found")
    await owned_group(db, visible.group_session_id, actor)
    group_run = await db.scalar(select(GroupRun).where(
        GroupRun.id == group_run_id
    ).with_for_update())
    if if_match is not None and expected(if_match) != group_run.version:
        raise HTTPException(status_code=409, detail={"code": "version_conflict"})
    run = await db.scalar(select(Run).where(Run.id == group_run.run_id).with_for_update())
    member = await db.scalar(select(GroupRunMember).where(
        GroupRunMember.group_run_id == group_run.id,
        GroupRunMember.agent_id == body.reporter_agent_id,
        GroupRunMember.participation_status == "available",
    ))
    if member is None:
        raise HTTPException(status_code=422, detail={"code": "reporter_unavailable"})
    if run.status != "waiting_user" or run.lifecycle != "waiting":
        raise HTTPException(status_code=409, detail={"code": "domain_validation"})
    group_run.reporter_agent_id = body.reporter_agent_id
    group_run.version += 1
    run.status = "active"
    run.lifecycle = "running"
    run.wait_reason = None
    run.version += 1
    await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id,
        group_session_id=group_run.group_session_id, group_run_id=group_run.id,
        event_type="group.reporter_replaced",
        event_dedupe_key=f"replace_group_reporter:{record.id}",
        actor_type="user", actor_id=run.user_id,
        entity_type="group_run", entity_id=group_run.id,
        entity_version=group_run.version, visibility="group",
        summary="Group Reporter replaced by user",
        payload={"reporter_agent_id": body.reporter_agent_id},
        correlation_id=run.id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.control", action="evaluate_group_quiescence",
        aggregate_type="group_run", aggregate_id=group_run.id,
        partition_key=run.session_id,
        dedupe_key=f"reporter_replaced:{record.id}:quiescence",
        payload_ref={"group_run_id": group_run.id},
    )
    response = await native_group_run_view(db, group_run)
    record.resource_type = "group_run"
    record.resource_id = group_run.id
    record.http_status = 202
    record.response_snapshot = response
    await db.commit()
    return response


def _shared_task_view(row: SharedTask) -> dict:
    return {
        "id": row.id, "group_session_id": row.group_session_id,
        "group_run_id": row.group_run_id, "title": row.title,
        "description": row.description, "status": row.status,
        "assignee_agent_id": row.assignee_agent_id,
        "depends_on_task_ids": list(row.depends_on_task_ids or []),
        "result_refs": list(row.result_refs or []), "version": row.version,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


@router.get("/groups/{group_id}/shared-tasks")
async def list_shared_tasks(
    group_id: str,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    group = await owned_group(db, group_id, actor)
    rows = list(await db.scalars(select(SharedTask).where(
        SharedTask.group_session_id == group.id
    ).order_by(SharedTask.created_at, SharedTask.id)))
    return {"items": [_shared_task_view(item) for item in rows], "next_cursor": None}


@router.post("/groups/{group_id}/shared-tasks", status_code=201)
async def create_shared_task(
    group_id: str, body: SharedTaskCreateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    group = await owned_group(db, group_id, actor)
    record, replay = await _idempotency(
        db, actor=actor, operation="create_shared_task", key=idempotency_key,
        request={"group_id": group_id, **body.model_dump()},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    if body.assignee_agent_id:
        member = await db.scalar(select(GroupMembership).where(
            GroupMembership.group_session_id == group.id,
            GroupMembership.agent_id == body.assignee_agent_id,
            GroupMembership.status == "joined",
        ))
        if member is None:
            raise HTTPException(status_code=422, detail={"code": "group_member_unavailable"})
    if body.group_run_id:
        owned_run = await db.scalar(select(GroupRun.id).where(
            GroupRun.id == body.group_run_id,
            GroupRun.group_session_id == group.id,
        ))
        if owned_run is None:
            raise HTTPException(status_code=422, detail={"code": "invalid_group_run_reference"})
    if body.depends_on_task_ids:
        dependency_count = int(await db.scalar(
            select(func.count()).select_from(SharedTask).where(
                SharedTask.group_session_id == group.id,
                SharedTask.id.in_(set(body.depends_on_task_ids)),
            )
        ) or 0)
        if dependency_count != len(set(body.depends_on_task_ids)):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_shared_task_reference"},
            )
    task = SharedTask(
        id=uuid7(), group_session_id=group.id, group_run_id=body.group_run_id,
        title=body.title.strip(), description=body.description,
        status="pending", assignee_agent_id=body.assignee_agent_id,
        depends_on_task_ids=list(dict.fromkeys(body.depends_on_task_ids)),
        result_refs=[],
    )
    if task.id in task.depends_on_task_ids:
        raise HTTPException(status_code=422, detail={"code": "domain_validation"})
    db.add(task)
    await db.flush()
    response = _shared_task_view(task)
    record.resource_type = "shared_task"
    record.resource_id = task.id
    record.http_status = 201
    record.response_snapshot = response
    await db.commit()
    return response


@router.patch("/shared-tasks/{shared_task_id}")
async def update_shared_task(
    shared_task_id: str, body: SharedTaskPatchRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    visible = await db.get(SharedTask, shared_task_id)
    if visible is None:
        raise HTTPException(status_code=404, detail="SharedTask not found")
    await owned_group(db, visible.group_session_id, actor)
    record, replay = await _idempotency(
        db, actor=actor, operation="update_shared_task", key=idempotency_key,
        request={"shared_task_id": shared_task_id, **body.model_dump()},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    task = await db.scalar(select(SharedTask).where(SharedTask.id == shared_task_id).with_for_update())
    if if_match is not None and expected(if_match) != task.version:
        raise HTTPException(status_code=409, detail={"code": "version_conflict"})
    values = body.model_dump(exclude_unset=True)
    if values.get("status") not in {None, "pending", "in_progress", "completed", "blocked", "cancelled"}:
        raise HTTPException(status_code=422, detail={"code": "domain_validation"})
    dependencies = values.get("depends_on_task_ids")
    if dependencies is not None and task.id in dependencies:
        raise HTTPException(status_code=422, detail={"code": "domain_validation"})
    for key, value in values.items():
        if key in {"depends_on_task_ids", "result_refs"} and value is not None:
            value = list(dict.fromkeys(value if key == "depends_on_task_ids" else [str(item) for item in value])) if key == "depends_on_task_ids" else value
        setattr(task, key, value)
    task.version += 1
    await db.flush()
    response = _shared_task_view(task)
    record.resource_type = "shared_task"
    record.resource_id = task.id
    record.http_status = 200
    record.response_snapshot = response
    await db.commit()
    return response


async def _cancel_group_run_state(
    db: AsyncSession, group_run: GroupRun, run: Run, *, reason: str, event_key: str,
) -> None:
    now = utcnow()
    work_items = list(await db.scalars(select(WorkItem).where(
        WorkItem.run_id == run.id
    ).with_for_update()))
    for work in work_items:
        if work.status not in NON_TERMINAL_WORK: continue
        runners = list(await db.scalars(select(DurableRunner).where(
            DurableRunner.work_item_id == work.id,
            DurableRunner.status.in_(("queued", "running")),
        ).with_for_update()))
        for runner in runners:
            runner.status = "cancelled"; runner.lease_expires_at = None; runner.version += 1
        invocations = list(await db.scalars(select(ToolInvocation).where(
            ToolInvocation.work_item_id == work.id,
            ToolInvocation.status.in_(("created", "waiting_confirmation", "waiting_resource", "queued", "running", "cancel_requested")),
        ).with_for_update()))
        for invocation in invocations:
            invocation.status = "cancelled"; invocation.error_code = "run_cancelled"; invocation.version += 1
            confirmation = await db.scalar(select(ToolConfirmation).where(
                ToolConfirmation.tool_invocation_id == invocation.id,
                ToolConfirmation.status == "pending",
            ).with_for_update())
            if confirmation:
                confirmation.status = "cancelled"; confirmation.decided_at = now; confirmation.version += 1
        clarifications = list(await db.scalars(select(Clarification).where(
            Clarification.work_item_id == work.id, Clarification.status == "open",
        ).with_for_update()))
        for clarification in clarifications:
            clarification.status = "cancelled"; clarification.version += 1
        agent_invocations = list(await db.scalars(select(AgentInvocation).where(
            (AgentInvocation.caller_work_item_id == work.id) | (AgentInvocation.target_work_item_id == work.id),
            AgentInvocation.status.in_(("created", "queued", "running")),
        ).with_for_update()))
        for invocation in agent_invocations:
            invocation.status = "cancelled"; invocation.error_code = "run_cancelled"; invocation.version += 1
        work.status = "cancelled"; work.active_runner_id = None
        work.error_code = "run_cancelled"; work.version += 1
    run.lifecycle = "cancelled"; run.status = "cancelled"
    run.wait_reason = None; run.outcome = None
    run.cancellation_requested_at = now; run.finished_at = now
    run.error_code = "cancelled_by_user"; run.result_summary = reason; run.version += 1
    cancelled_event = await ExecutionEventWriter(db).append(session_id=run.session_id, run_id=run.id,
        group_session_id=group_run.group_session_id, group_run_id=group_run.id,
        event_type="run.cancelled", event_dedupe_key=event_key, actor_type="user", actor_id=run.user_id,
        entity_type="run", entity_id=run.id, entity_version=run.version, visibility="group",
        summary="GroupRun cancelled", payload={"reason": reason}, correlation_id=run.id)
    await create_notification_delivery_if_channel(
        db, session_id=run.session_id, triggering_event_id=cancelled_event.id,
        event_type="run_cancelled", run_id=run.id, group_run_id=group_run.id,
    )


@router.post("/group-runs/{group_run_id}/cancel", status_code=202)
async def cancel_group_run(
    group_run_id: str, body: GroupCloseRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    record, replay = await _idempotency(db, actor=actor, operation="cancel_group_run",
        key=idempotency_key, request={"group_run_id": group_run_id, "reason": body.reason})
    if replay and record.response_snapshot: return record.response_snapshot
    visible = await db.get(GroupRun, group_run_id)
    if visible is None: raise HTTPException(status_code=404, detail="GroupRun not found")
    group = await owned_group(db, visible.group_session_id, actor)
    await db.scalar(select(UserSession).where(UserSession.id == group.parent_session_id).with_for_update())
    group_run = await db.scalar(select(GroupRun).where(GroupRun.id == group_run_id).with_for_update())
    run = await db.scalar(select(Run).where(Run.id == group_run.run_id).with_for_update())
    if run.version != expected(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": run.version})
    if run.lifecycle in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail={"code": "run_terminal", "lifecycle": run.lifecycle})
    await _cancel_group_run_state(db, group_run, run, reason=body.reason,
                                  event_key=f"command:cancel_group_run:{record.id}")
    response = await group_run_view(db, group_run)
    record.resource_type, record.resource_id, record.http_status = "group_run", group_run.id, 200
    record.response_snapshot = response; await db.commit(); return response


@router.post("/groups/{group_id}/close", status_code=202)
async def close_group(
    group_id: str, body: GroupCloseRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    record, replay = await _idempotency(db, actor=actor, operation="close_group", key=idempotency_key,
        request={"group_id": group_id, "reason": body.reason})
    if replay and record.response_snapshot: return record.response_snapshot
    visible = await owned_group(db, group_id, actor)
    await db.scalar(select(UserSession).where(UserSession.id == visible.parent_session_id).with_for_update())
    group = await db.scalar(select(GroupSession).where(GroupSession.id == group_id).with_for_update())
    if group.version != expected(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": group.version})
    if group.status != "open":
        raise HTTPException(status_code=409, detail={"code": "group_not_open", "status": group.status})
    active = list((await db.execute(select(GroupRun, Run).join(Run, Run.id == GroupRun.run_id).where(
        GroupRun.group_session_id == group.id,
        Run.lifecycle.not_in(("completed", "cancelled")),
    ).with_for_update())).all())
    for group_run, run in active:
        await _cancel_group_run_state(db, group_run, run, reason=body.reason,
                                      event_key=f"command:close_group:{record.id}:run:{run.id}")
    group.status = "closed"; group.closed_at = utcnow(); group.version += 1
    await ExecutionEventWriter(db).append(session_id=group.parent_session_id,
        group_session_id=group.id, event_type="group.closed",
        event_dedupe_key=f"command:close_group:{record.id}", actor_type="user", actor_id=group.user_id,
        entity_type="group_session", entity_id=group.id, entity_version=group.version,
        visibility="group", summary="Group closed", payload={"reason": body.reason}, correlation_id=group.id)
    response = {"id": group.id, "status": group.status, "version": group.version,
                "closed_at": group.closed_at.isoformat()}
    record.resource_type, record.resource_id, record.http_status = "group_session", group.id, 200
    record.response_snapshot = response; await db.commit(); return response
