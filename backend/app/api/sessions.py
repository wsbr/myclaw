from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import SessionActor, get_session_actor
from app.models.agent_v1 import AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord, UserSession
from app.models.runtime_v1 import Message, Run, SessionSandbox
from app.schemas.runtime_v1 import (
    ChatCommandResponse,
    MessageCreateRequest,
    MessageResponse,
    RunResponse,
    SessionCreateRequest,
    SessionSandboxResponse,
    SessionResponse,
)
from app.services.chat_service import AgentUnavailable, create_user_session, enqueue_chat_message
from app.services.visible_events import VisibleEventProjection, event_public_dict
from app.services.cursor_service import SortKey, paginate_scalars
from app.database import AsyncSessionLocal
from app.config import settings
from app.services.raw_identity_service import ensure_raw_user_account

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def _idempotency(
    db: AsyncSession, *, actor: SessionActor, operation: str,
    key: str, request: dict,
) -> tuple[IdempotencyRecord, bool]:
    digest = _hash(request)
    record = await db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.principal_type == actor.principal_type,
        IdempotencyRecord.principal_id == actor.principal_id,
        IdempotencyRecord.operation == operation,
        IdempotencyRecord.idempotency_key == key,
    ))
    if record:
        if record.request_hash != digest:
            raise HTTPException(status_code=409, detail={"code": "idempotency_mismatch"})
        return record, True
    record = IdempotencyRecord(
        id=uuid7(), principal_type=actor.principal_type, principal_id=actor.principal_id,
        operation=operation, idempotency_key=key, request_hash=digest,
        expires_at=utcnow() + timedelta(days=7),
    )
    db.add(record)
    await db.flush()
    return record, False


async def _owned_session(db: AsyncSession, session_id: str, actor: SessionActor) -> UserSession:
    session = await db.get(UserSession, session_id)
    allowed = session is not None and (
        (actor.api_key is None and actor.account is not None
         and session.user_id == actor.account.user_id)
        or (actor.api_key is not None and session.origin_api_key_id == actor.api_key.id
            and actor.account is not None and session.account_id == actor.account.id
            and session.entry_agent_id == actor.api_key.agent_id)
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def _session_response(db: AsyncSession, session: UserSession) -> SessionResponse:
    sandbox = await db.scalar(select(SessionSandbox).where(SessionSandbox.session_id == session.id))
    value = SessionResponse.model_validate(session).model_copy(
        update={"session_sandbox": SessionSandboxResponse.model_validate(sandbox) if sandbox else None}
    )
    return value


@router.get("", response_model=dict)
async def list_sessions(
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    if actor.account is None:
        raise HTTPException(status_code=403, detail={"code": "identity_mapping_required"})
    target_user_id = actor.account.user_id
    query = select(UserSession).where(UserSession.user_id == target_user_id)
    if actor.api_key:
        query = query.where(UserSession.origin_api_key_id == actor.api_key.id,
                            UserSession.entry_agent_id == actor.api_key.agent_id)
    rows, next_cursor = await paginate_scalars(
        db, query, sort=(SortKey(UserSession.updated_at), SortKey(UserSession.id)),
        limit=limit, cursor=cursor,
        principal=f"{actor.principal_type}:{actor.principal_id}", resource="sessions",
        filters={"user_id": target_user_id},
    )
    return {"items": [(await _session_response(db, row)).model_dump(mode="json") for row in rows],
            "next_cursor": next_cursor}


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(
    body: SessionCreateRequest, idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    expected_origin = "api" if actor.api_key else "web"
    if actor.api_key and body.entry_agent_id != actor.api_key.agent_id:
        raise HTTPException(status_code=403, detail={"code": "api_key_agent_mismatch"})
    if actor.api_key and actor.account is None:
        try:
            account, _ = await ensure_raw_user_account(db, actor.asserted_user_id)
        except ValueError:
            raise HTTPException(status_code=409, detail={"code": "identity_mapping_required"})
        actor = SessionActor(
            account=account, api_key=actor.api_key,
            asserted_user_id=actor.asserted_user_id,
        )
    if actor.account is None:
        raise HTTPException(status_code=403, detail={"code": "identity_mapping_required"})
    target_user_id = actor.account.user_id
    agent = await db.get(AgentBlueprint, body.entry_agent_id)
    if agent is None or (not actor.api_key and agent.visibility == "private" and agent.owner_account_id != actor.account.id and "admin" not in actor.roles):
        raise HTTPException(status_code=404, detail="Agent not found")
    record, replay = await _idempotency(
        db, actor=actor, operation="create_session", key=idempotency_key,
        request={"entry_agent_id": body.entry_agent_id},
    )
    if replay and record.response_snapshot:
        return SessionResponse.model_validate(record.response_snapshot)
    try:
        session = await create_user_session(
            db, user_id=target_user_id, entry_agent_id=body.entry_agent_id,
            origin_type=expected_origin, origin_api_key_id=actor.api_key.id if actor.api_key else None,
        )
    except AgentUnavailable as error:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "agent_unavailable", "message": str(error)})
    response = await _session_response(db, session)
    record.resource_type, record.resource_id, record.http_status = "user_session", session.id, 201
    record.response_snapshot = response.model_dump(mode="json")
    await db.commit()
    return response


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str, db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    return await _session_response(db, await _owned_session(db, session_id, actor))


@router.delete("/{session_id}", status_code=204)
async def close_session(
    session_id: str, idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    session = await _owned_session(db, session_id, actor)
    record, replay = await _idempotency(
        db, actor=actor, operation="close_session", key=idempotency_key,
        request={"session_id": session_id},
    )
    if not replay:
        session.status = "closed"
        session.closed_at = utcnow()
        session.version += 1
        record.resource_type, record.resource_id, record.http_status = "user_session", session.id, 204
        record.response_snapshot = {"closed": True}
        await db.commit()
    return None


@router.get("/{session_id}/messages", response_model=dict)
async def list_messages(
    session_id: str, scope: str = "chat", limit: int = 100, cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    await _owned_session(db, session_id, actor)
    if scope not in {"chat", "group", "all"}:
        raise HTTPException(status_code=422, detail={"code": "invalid_message_scope"})
    query = select(Message).where(Message.session_id == session_id)
    if scope != "all":
        query = query.where(Message.scope_type == scope)
    rows, next_cursor = await paginate_scalars(
        db, query,
        sort=(SortKey(Message.created_at, descending=False), SortKey(Message.id, descending=False)),
        limit=limit, cursor=cursor,
        principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=f"session:{session_id}:messages", filters={"scope": scope},
    )
    return {"items": [MessageResponse.model_validate(row).model_dump(mode="json") for row in rows],
            "next_cursor": next_cursor}


@router.post("/{session_id}/messages", response_model=ChatCommandResponse, status_code=202)
async def create_message(
    session_id: str, body: MessageCreateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    session = await _owned_session(db, session_id, actor)
    record, replay = await _idempotency(
        db, actor=actor, operation="create_chat_run", key=idempotency_key,
        request={"session_id": session_id, "content": body.content},
    )
    if replay and record.response_snapshot:
        return ChatCommandResponse.model_validate(record.response_snapshot)
    try:
        message, run, _, _ = await enqueue_chat_message(
            db, session_id=session_id, user_id=session.user_id,
            content=body.content, idempotency_record_id=record.id,
        )
    except (ValueError, RuntimeError, AgentUnavailable) as error:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "chat_command_rejected", "message": str(error)})
    response = ChatCommandResponse(
        message=MessageResponse.model_validate(message), run=RunResponse.model_validate(run),
        required_actions=[], links={"events": f"/api/v1/sessions/{session_id}/events/stream"},
    )
    record.resource_type, record.resource_id, record.http_status = "run", run.id, 202
    record.response_snapshot = response.model_dump(mode="json")
    await db.commit()
    return response


@router.get("/{session_id}/events")
async def list_events(
    session_id: str, after_sequence: int = 0, scan_limit: int = 500, limit: int = 100,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    session = await _owned_session(db, session_id, actor)
    try:
        page = await VisibleEventProjection(db).scan_session_user(
            user_id=session.user_id, session_id=session_id,
            after_sequence=after_sequence, scan_limit=scan_limit, limit=limit,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "items": [event_public_dict(event) for event in page.items],
        "scanned_through_sequence": page.scanned_through_sequence,
        "session_high_watermark": page.session_high_watermark,
        "has_more": page.has_more,
    }


@router.get("/{session_id}/events/stream")
async def stream_events(
    session_id: str, request: Request, after_sequence: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    # Authorize before starting the streaming response.
    session = await _owned_session(db, session_id, actor)
    try:
        cursor = max(after_sequence, int(last_event_id or 0))
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "invalid_event_cursor"})
    user_id = session.user_id

    async def generate():
        nonlocal cursor
        redis_client = pubsub = None
        try:
            try:
                import redis.asyncio as aioredis
                redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
                pubsub = redis_client.pubsub()
                await pubsub.subscribe(f"myclaw:session:{session_id}:live")
            except Exception:
                if pubsub is not None:
                    await pubsub.aclose()
                if redis_client is not None:
                    await redis_client.aclose()
                redis_client = pubsub = None
            while not await request.is_disconnected():
                async with AsyncSessionLocal() as stream_db:
                    page = await VisibleEventProjection(stream_db).scan_session_user(
                        user_id=user_id, session_id=session_id, after_sequence=cursor,
                        scan_limit=500, limit=100,
                    )
                    emitted_sequence = cursor
                    if page.items:
                        for event in page.items:
                            data = json.dumps(event_public_dict(event), ensure_ascii=False, separators=(",", ":"))
                            yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"
                            emitted_sequence = event.sequence
                    if page.scanned_through_sequence > cursor:
                        if page.scanned_through_sequence > emitted_sequence:
                            yield (
                                f"id: {page.scanned_through_sequence}\nevent: cursor\n"
                                f"data: {{\"scanned_through_sequence\":{page.scanned_through_sequence}}}\n\n"
                            )
                        cursor = page.scanned_through_sequence
                if pubsub is not None:
                    live = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                    if live and live.get("type") == "message":
                        yield f"event: model.delta\ndata: {live['data']}\n\n"
                    else:
                        yield ": keepalive\n\n"
                else:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(1)
        finally:
            if pubsub is not None:
                await pubsub.unsubscribe(f"myclaw:session:{session_id}:live")
                await pubsub.aclose()
            if redis_client is not None:
                await redis_client.aclose()

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
