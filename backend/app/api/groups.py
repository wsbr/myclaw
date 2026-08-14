from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.sessions import _idempotency, _owned_session
from app.database import get_db
from app.deps import SessionActor, get_session_actor
from app.models.common import utcnow, uuid7
from app.models.foundation import ExecutionEvent
from app.models.group_v1 import (
    GroupDiagnostic, GroupRun, GroupSession, GroupMembership, GroupRunMember,
    GroupSummaryRequest, SharedTask,
)
from app.models.runtime_v1 import (
    AgentInvocation, Clarification, DurableRunner, Message, Run, ToolConfirmation,
    ToolInvocation, WorkItem,
)
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.services.notification_delivery_service import create_notification_delivery_if_channel
from app.schemas.group_v1 import (
    GroupCloseRequest, GroupRunCreateRequest, GroupRunMessageRequest, GroupCreateRequest,
    GroupReporterReplaceRequest, SharedTaskCreateRequest, SharedTaskPatchRequest,
)
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
from app.services.group_diagnostic_service import group_diagnostic_view

router = APIRouter(tags=["groups"])
NON_TERMINAL_WORK = {"queued", "running", "waiting_user", "waiting_agent", "waiting_tool", "waiting_resource"}


def expected(value: str | None) -> int:
    raw = (value or "").replace("W/", "").strip('" ')
    if not raw.isdigit(): raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    return int(raw)


async def owned_group(db: AsyncSession, group_id: str, actor: SessionActor, *, lock: bool = False) -> GroupSession:
    query = select(GroupSession).where(GroupSession.id == group_id)
    if lock: query = query.with_for_update()
    group = await db.scalar(query)
    if group is None: raise HTTPException(status_code=404, detail="Group not found")
    await _owned_session(db, group.session_id, actor)
    return group


async def group_run_view(db: AsyncSession, group_run: GroupRun) -> dict:
    return await native_group_run_view(db, group_run)


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
        GroupSession.session_id == session.id,
        GroupSession.status.in_(("open", "closed", "archived")),
    ).order_by(GroupSession.created_at.desc(), GroupSession.id.desc())))
    return {"items": [group_session_view(item) for item in rows], "next_cursor": None}


@router.get("/groups/{group_id}")
async def get_group(group_id: str, db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor)):
    group = await owned_group(db, group_id, actor)
    return group_session_view(group)


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


@router.get("/groups/{group_id}/timeline")
async def get_group_timeline(
    group_id: str, after_sequence: int = 0, limit: int = 100,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    group = await owned_group(db, group_id, actor)
    run_ids = select(GroupRun.run_id).where(GroupRun.group_session_id == group.id)
    rows = list(await db.scalars(select(ExecutionEvent).where(
        ExecutionEvent.session_id == group.session_id,
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


@router.get("/group-runs/{group_run_id}/diagnostics")
async def list_group_run_diagnostics(
    group_run_id: str,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    """Return append-only, user-safe Runtime decisions for one GroupRun."""

    group_run = await db.get(GroupRun, group_run_id)
    if group_run is None:
        raise HTTPException(status_code=404, detail="GroupRun not found")
    await owned_group(db, group_run.group_session_id, actor)
    rows = list(await db.scalars(select(GroupDiagnostic).where(
        GroupDiagnostic.group_run_id == group_run.id,
    ).order_by(
        GroupDiagnostic.created_at.desc(), GroupDiagnostic.id.desc(),
    ).limit(max(1, min(limit, 100)))))
    return {
        "items": [group_diagnostic_view(item) for item in rows],
        "next_cursor": None,
    }


@router.get("/group-runs/{group_run_id}/interaction-graph")
async def get_group_interaction_graph(
    group_run_id: str,
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    """Project persisted collaboration facts for exactly one GroupRun."""
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
        Message.status == "committed",
    ).order_by(Message.created_at, Message.id)))
    invocations = list(await db.scalars(select(AgentInvocation).where(
        AgentInvocation.group_run_id == group_run.id,
    ).order_by(AgentInvocation.created_at, AgentInvocation.id)))
    invocation_result_message_ids = {
        item.result_message_id for item in invocations if item.result_message_id
    }
    invocation_request_message_ids = {
        str((item.context_snapshot or {}).get("request_message_id"))
        for item in invocations
        if (item.context_snapshot or {}).get("request_message_id")
    }
    edges = []
    for item in messages:
        if item.sender_agent_id not in member_ids:
            continue
        # AgentInvocation projects a human-visible directed request and result
        # into the transcript. The graph folds both into its single durable
        # invocation edge so one collaboration is not counted three times.
        if item.id in (
            invocation_request_message_ids | invocation_result_message_ids
        ):
            continue
        if item.visibility == "group" and item.recipient_agent_id is None:
            edges.append({
                "source_agent_id": item.sender_agent_id,
                "target_agent_id": None,
                "target_node_id": f"group:{group_run.id}",
                "cause_type": "group_message",
                "cause_id": item.id,
                "status": "committed",
                "created_at": item.created_at.isoformat(),
                "result_message_id": None,
                "completed_at": None,
            })
        elif item.recipient_agent_id in member_ids:
            edges.append({
                "source_agent_id": item.sender_agent_id,
                "target_agent_id": item.recipient_agent_id,
                "target_node_id": item.recipient_agent_id,
                "cause_type": "message",
                "cause_id": item.id,
                "status": "committed",
                "created_at": item.created_at.isoformat(),
                "result_message_id": None,
                "completed_at": None,
            })
    edges.extend({
        "source_agent_id": item.caller_agent_id,
        "target_agent_id": item.target_agent_id,
        "target_node_id": item.target_agent_id,
        "cause_type": "agent_invocation",
        "cause_id": item.id,
        "status": item.status,
        "created_at": item.created_at.isoformat(),
        "result_message_id": item.result_message_id,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
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
                 "recipient_agent_id": body.recipient_agent_id,
                 "reporter_agent_id": body.reporter_agent_id,
                 "reference_run_ids": sorted(set(body.reference_run_ids))},
    )
    if replay and record.response_snapshot: return record.response_snapshot
    visible = await owned_group(db, group_id, actor)
    if actor.account is None:
        raise HTTPException(status_code=403, detail={"code": "identity_mapping_required"})
    session = await _owned_session(db, visible.session_id, actor)
    try:
        group_run = await create_followup_group_run(
            db, session=session, group=visible, account_id=actor.account.id,
            content=content, recipient_agent_id=body.recipient_agent_id,
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


@router.post("/group-runs/{group_run_id}/messages", status_code=202)
async def create_group_run_message(
    group_run_id: str, body: GroupRunMessageRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    content = body.content.strip()
    record, replay = await _idempotency(db, actor=actor, operation="create_group_run_message",
        key=idempotency_key, request={"group_run_id": group_run_id, "content": content,
                                      "recipient_agent_id": body.recipient_agent_id})
    if replay and record.response_snapshot: return record.response_snapshot
    visible = await db.get(GroupRun, group_run_id)
    if visible is None:
        raise HTTPException(status_code=404, detail="GroupRun not found")
    group = await owned_group(db, visible.group_session_id, actor)
    session = await _owned_session(db, group.session_id, actor)
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
                       "recipient_agent_id": row.recipient_agent_id,
                       "visibility": row.visibility,
                       "role": row.role, "content": row.content,
                       "group_run_id": row.group_run_id,
                       "sequence_no": row.sequence_no,
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
    if run.status != "waiting_user":
        raise HTTPException(status_code=409, detail={"code": "domain_validation"})
    group_run.reporter_agent_id = body.reporter_agent_id
    group_run.version += 1
    run.status = "active"
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
    run.status = "cancelled"; run.wait_reason = None
    run.cancellation_requested_at = now
    run.completed_at = now; run.terminal_reason = "cancelled_by_user"
    run.version += 1
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
    await db.scalar(select(UserSession).where(UserSession.id == group.session_id).with_for_update())
    group_run = await db.scalar(select(GroupRun).where(GroupRun.id == group_run_id).with_for_update())
    run = await db.scalar(select(Run).where(Run.id == group_run.run_id).with_for_update())
    if run.version != expected(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": run.version})
    if run.status in {"completed", "cancelled", "failed"}:
        raise HTTPException(status_code=409, detail={"code": "run_terminal", "status": run.status})
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
    await db.scalar(select(UserSession).where(UserSession.id == visible.session_id).with_for_update())
    group = await db.scalar(select(GroupSession).where(GroupSession.id == group_id).with_for_update())
    if group.version != expected(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": group.version})
    if group.status != "open":
        raise HTTPException(status_code=409, detail={"code": "group_not_open", "status": group.status})
    active = list((await db.execute(select(GroupRun, Run).join(Run, Run.id == GroupRun.run_id).where(
        GroupRun.group_session_id == group.id,
        Run.status.not_in(("completed", "cancelled", "failed")),
    ).with_for_update())).all())
    for group_run, run in active:
        await _cancel_group_run_state(db, group_run, run, reason=body.reason,
                                      event_key=f"command:close_group:{record.id}:run:{run.id}")
    group.status = "closed"; group.closed_at = utcnow(); group.version += 1
    await ExecutionEventWriter(db).append(session_id=group.session_id,
        group_session_id=group.id, event_type="group.closed",
        event_dedupe_key=f"command:close_group:{record.id}", actor_type="user", actor_id=group.user_id,
        entity_type="group_session", entity_id=group.id, entity_version=group.version,
        visibility="group", summary="Group closed", payload={"reason": body.reason}, correlation_id=group.id)
    response = {"id": group.id, "status": group.status, "version": group.version,
                "closed_at": group.closed_at.isoformat()}
    record.resource_type, record.resource_id, record.http_status = "group_session", group.id, 200
    record.response_snapshot = response; await db.commit(); return response
