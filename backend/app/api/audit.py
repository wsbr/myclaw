"""Role-scoped PlatformAuditEvent query endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import CurrentAccount, get_current_account, get_current_admin, get_current_builder
from app.models.agent_v1 import AgentBlueprint
from app.models.foundation import PlatformAuditEvent
from app.models.registry import RegisteredSkill
from app.services.cursor_service import SortKey, paginate_scalars


router = APIRouter(tags=["audit"])


def _view(row: PlatformAuditEvent) -> dict:
    return {
        "id": row.id,
        "occurred_at": row.occurred_at,
        "category": row.category,
        "action": row.action,
        "actor_safe": {
            "type": row.actor_type,
            "id": row.actor_id,
            "user_id": row.actor_user_id,
        },
        # Compatibility fields used by the current Admin table.
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "resource": {"type": row.resource_type, "id": row.resource_id, "version": row.resource_version},
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "resource_version": row.resource_version,
        "outcome": row.outcome,
        "severity": row.severity,
        "summary": row.summary,
        "changes_redacted": row.changes,
        "changes": row.changes,
    }


def _filters(
    query, *, category: str | None, action: str | None, resource_type: str | None,
    resource_id: str | None, actor_id: str | None, outcome: str | None,
    from_at: datetime | None, to_at: datetime | None,
):
    if category:
        query = query.where(PlatformAuditEvent.category == category)
    if action:
        query = query.where(PlatformAuditEvent.action == action)
    if resource_type:
        query = query.where(PlatformAuditEvent.resource_type == resource_type)
    if resource_id:
        query = query.where(PlatformAuditEvent.resource_id == resource_id)
    if actor_id:
        query = query.where(PlatformAuditEvent.actor_id == actor_id)
    if outcome:
        query = query.where(PlatformAuditEvent.outcome == outcome)
    if from_at:
        query = query.where(PlatformAuditEvent.occurred_at >= from_at)
    if to_at:
        query = query.where(PlatformAuditEvent.occurred_at <= to_at)
    return query


async def _page(
    db: AsyncSession, query, limit: int, cursor: str | None, *,
    current: CurrentAccount, resource: str, filters: dict,
) -> dict:
    rows, next_cursor = await paginate_scalars(
        db, query,
        sort=(SortKey(PlatformAuditEvent.occurred_at), SortKey(PlatformAuditEvent.id)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource=resource, filters=filters,
    )
    return {"items": [_view(row) for row in rows], "next_cursor": next_cursor}


@router.get("/admin/audit-events")
async def list_admin_audit_events(
    category: str | None = None, action: str | None = None,
    resource_type: str | None = None, resource_id: str | None = None,
    actor_id: str | None = None, outcome: str | None = None,
    from_at: datetime | None = Query(default=None, alias="from"),
    to_at: datetime | None = Query(default=None, alias="to"), limit: int = 100,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_admin),
):
    filters = {
        "category": category, "action": action, "resource_type": resource_type,
        "resource_id": resource_id, "actor_id": actor_id, "outcome": outcome,
        "from": from_at, "to": to_at,
    }
    query = _filters(
        select(PlatformAuditEvent), category=category, action=action,
        resource_type=resource_type, resource_id=resource_id, actor_id=actor_id,
        outcome=outcome, from_at=from_at, to_at=to_at,
    )
    return await _page(
        db, query, limit, cursor, current=current,
        resource="admin:audit-events", filters=filters,
    )


@router.get("/accounts/me/audit-events")
async def list_my_audit_events(
    category: str | None = None, limit: int = 100, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    if category and category not in {"identity", "security"}:
        raise HTTPException(status_code=422, detail={"code": "unsupported_audit_category"})
    query = select(PlatformAuditEvent).where(
        PlatformAuditEvent.category.in_(("identity", "security")),
        or_(
            (PlatformAuditEvent.actor_type == "account") & (PlatformAuditEvent.actor_id == current.account.id),
            PlatformAuditEvent.actor_user_id == current.account.user_id,
            (PlatformAuditEvent.resource_type == "platform_account") & (PlatformAuditEvent.resource_id == current.account.id),
        ),
    )
    if category:
        query = query.where(PlatformAuditEvent.category == category)
    return await _page(
        db, query, limit, cursor, current=current,
        resource="accounts:me:audit-events", filters={"category": category},
    )


@router.get("/builder/audit-events")
async def list_builder_audit_events(
    resource_type: str, resource_id: str | None = None, limit: int = 100,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    if resource_type not in {"agent", "skill"}:
        raise HTTPException(status_code=422, detail={"code": "unsupported_resource_type"})
    if resource_type == "agent":
        owned_ids = select(AgentBlueprint.id).where(AgentBlueprint.owner_account_id == current.account.id)
    else:
        owned_ids = select(RegisteredSkill.id).where(RegisteredSkill.owner_account_id == current.account.id)
    query = select(PlatformAuditEvent).where(
        PlatformAuditEvent.resource_type == resource_type,
        PlatformAuditEvent.resource_id.in_(owned_ids),
    )
    if resource_id:
        query = query.where(PlatformAuditEvent.resource_id == resource_id)
    return await _page(
        db, query, limit, cursor, current=current,
        resource="builder:audit-events",
        filters={"resource_type": resource_type, "resource_id": resource_id},
    )
