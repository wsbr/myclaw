from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import CurrentAccount, get_current_account
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord
from app.models.memory_v1 import MemoryHead, MemoryRecord
from app.persistence.reliable_writes import PlatformAuditWriter
from app.services.memory_service import memory_view, normalized_content
from app.services.cursor_service import SortKey, paginate_scalars

router = APIRouter(prefix="/memories", tags=["memories"])


class MemoryPatch(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def _idem(db, current, operation, key, request):
    digest = _digest(request)
    record = await db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.principal_type == "account",
        IdempotencyRecord.principal_id == current.account.id,
        IdempotencyRecord.operation == operation,
        IdempotencyRecord.idempotency_key == key,
    ))
    if record:
        if record.request_hash != digest:
            raise HTTPException(status_code=409, detail={"code": "idempotency_mismatch"})
        return record, True
    record = IdempotencyRecord(id=uuid7(), principal_type="account", principal_id=current.account.id,
        operation=operation, idempotency_key=key, request_hash=digest,
        expires_at=utcnow() + timedelta(days=7))
    db.add(record); await db.flush(); return record, False


def _expected(value: str | None) -> int:
    raw = (value or "").replace("W/", "").strip(' "')
    if not raw.isdigit():
        raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    return int(raw)


async def _owned_memory(db, memory_id, current, *, lock=False):
    query = select(MemoryRecord).where(
        MemoryRecord.id == memory_id, MemoryRecord.user_id == current.account.user_id,
        MemoryRecord.scope.in_(("user", "user_agent")),
    )
    if lock: query = query.with_for_update()
    memory = await db.scalar(query)
    if memory is None: raise HTTPException(status_code=404, detail="Memory not found")
    return memory


@router.get("")
async def list_memories(
    scope: str | None = None, agent_id: str | None = None, status: str = "active",
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    if scope not in (None, "user", "user_agent"):
        raise HTTPException(status_code=422, detail={"code": "invalid_memory_scope"})
    if status not in ("active", "superseded", "deleted"):
        raise HTTPException(status_code=422, detail={"code": "invalid_memory_filter"})
    statement = select(MemoryRecord).where(
        MemoryRecord.user_id == current.account.user_id,
        MemoryRecord.scope.in_(("user", "user_agent")), MemoryRecord.status == status,
    )
    if scope: statement = statement.where(MemoryRecord.scope == scope)
    if agent_id: statement = statement.where(MemoryRecord.agent_id == agent_id)
    rows, next_cursor = await paginate_scalars(
        db, statement, sort=(SortKey(MemoryRecord.updated_at), SortKey(MemoryRecord.id)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource="memories", filters={"scope": scope, "agent_id": agent_id, "status": status},
    )
    return {"items": [await memory_view(db, row) for row in rows],
            "next_cursor": next_cursor}


@router.get("/{memory_id}")
async def get_memory(memory_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account)):
    return await memory_view(db, await _owned_memory(db, memory_id, current))


@router.get("/{memory_id}/versions")
async def list_memory_versions(memory_id: str, cursor: str | None = None, limit: int = 50,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account)):
    anchor = await _owned_memory(db, memory_id, current)
    statement = select(MemoryRecord).where(
        MemoryRecord.head_id == anchor.head_id, MemoryRecord.user_id == current.account.user_id,
    )
    rows, next_cursor = await paginate_scalars(
        db, statement, sort=(SortKey(MemoryRecord.updated_at), SortKey(MemoryRecord.id)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource=f"memory:{anchor.head_id}:versions", filters={},
    )
    return {"items": [await memory_view(db, row) for row in rows],
            "next_cursor": next_cursor}


@router.patch("/{memory_id}")
async def edit_memory(memory_id: str, body: MemoryPatch,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account)):
    record, replay = await _idem(db, current, "edit_memory", idempotency_key,
        {"memory_id": memory_id, "content": body.content, "if_match": if_match})
    if replay and record.response_snapshot: return record.response_snapshot
    anchor = await _owned_memory(db, memory_id, current)
    head = await db.scalar(select(MemoryHead).where(MemoryHead.id == anchor.head_id).with_for_update())
    if head.version != _expected(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": head.version})
    active = await db.scalar(select(MemoryRecord).where(
        MemoryRecord.head_id == head.id, MemoryRecord.status == "active").with_for_update())
    if active is None or active.id != anchor.id:
        raise HTTPException(status_code=409, detail={"code": "memory_not_active"})
    content, digest = normalized_content(body.content)
    active.status = "superseded"; active.version += 1
    head.version += 1; head.last_user_edited_at = utcnow(); await db.flush()
    created = MemoryRecord(id=uuid7(), head_id=head.id, user_id=head.user_id, agent_id=head.agent_id,
        scope=head.scope, fact_key=head.fact_key, content=content, normalized_hash=digest,
        status="active", source_kind="user_edit", supersedes_memory_id=active.id)
    db.add(created); await db.flush()
    await PlatformAuditWriter(db).append(category="maintenance", action="memory.user_edited",
        actor_type="account", actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="memory", resource_id=created.id, resource_version=head.version,
        outcome="succeeded", summary="User edited a Memory fact",
        changes={"fields": ["content"]}, dedupe_key=f"audit:memory_edit:{record.id}")
    response = await memory_view(db, created, head)
    record.resource_type="memory"; record.resource_id=created.id; record.http_status=200
    record.response_snapshot=response; await db.commit(); return response


@router.delete("/{memory_id}", status_code=204)
async def delete_memory(memory_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account)):
    record, replay = await _idem(db, current, "delete_memory", idempotency_key,
        {"memory_id": memory_id, "if_match": if_match})
    if replay and record.response_snapshot: return Response(status_code=204)
    anchor = await _owned_memory(db, memory_id, current)
    head = await db.scalar(select(MemoryHead).where(MemoryHead.id == anchor.head_id).with_for_update())
    if head.version != _expected(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": head.version})
    active = await db.scalar(select(MemoryRecord).where(
        MemoryRecord.head_id == head.id, MemoryRecord.status == "active").with_for_update())
    if active is None or active.id != anchor.id:
        raise HTTPException(status_code=409, detail={"code": "memory_not_active"})
    active.status = "deleted"; active.version += 1; head.version += 1; head.last_user_edited_at = utcnow()
    await PlatformAuditWriter(db).append(category="maintenance", action="memory.user_deleted",
        actor_type="account", actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="memory", resource_id=active.id, resource_version=head.version,
        outcome="succeeded", summary="User deleted a Memory fact",
        changes={"fields": ["status"]}, dedupe_key=f"audit:memory_delete:{record.id}")
    record.resource_type="memory"; record.resource_id=active.id; record.http_status=204
    record.response_snapshot={"deleted": True}; await db.commit(); return Response(status_code=204)
