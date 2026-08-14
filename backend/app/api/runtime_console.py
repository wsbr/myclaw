"""Administrator-only, read-only Runtime management console API."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import CurrentAccount, get_current_admin
from app.models.foundation import OutboxCommand, UserSession
from app.models.runtime_v1 import Run
from app.services.cursor_service import SortKey, paginate_scalars
from app.services.runtime_console_service import (
    project_runtime_runs,
    runtime_console_summary,
    runtime_run_detail,
)


router = APIRouter(prefix="/admin/runtime", tags=["runtime-console"])


@router.get("/summary")
async def get_runtime_summary(
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_admin),
):
    del current
    return await runtime_console_summary(db)


@router.get("/runs")
async def list_runtime_runs(
    status: str | None = None,
    mode: str | None = None,
    account_id: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_admin),
):
    if mode is not None and mode not in {"chat", "plan", "group"}:
        raise HTTPException(status_code=422, detail={"code": "invalid_run_mode"})
    query = select(Run)
    if status:
        query = query.where(Run.status == status)
    if mode:
        query = query.where(Run.mode == mode)
    if account_id:
        query = query.where(Run.session_id.in_(select(UserSession.id).where(
            UserSession.account_id == account_id,
        )))
    filters = {"status": status, "mode": mode, "account_id": account_id}
    rows, next_cursor = await paginate_scalars(
        db,
        query,
        sort=(
            SortKey(Run.created_at, descending=True),
            SortKey(Run.id, descending=True),
        ),
        limit=max(1, min(limit, 50)),
        cursor=cursor,
        principal=f"account:{current.account.id}",
        resource="admin:runtime:runs",
        filters=filters,
    )
    return {
        "items": await project_runtime_runs(db, rows),
        "next_cursor": next_cursor,
    }


@router.get("/runs/{run_id}")
async def get_runtime_run_detail(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_admin),
):
    del current
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return await runtime_run_detail(db, run)


@router.get("/outbox")
async def list_runtime_outbox(
    topic: str | None = None,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_admin),
):
    query = select(OutboxCommand)
    if topic:
        query = query.where(OutboxCommand.topic == topic)
    if status:
        query = query.where(OutboxCommand.status == status)
    filters = {"topic": topic, "status": status}
    rows, next_cursor = await paginate_scalars(
        db,
        query,
        sort=(
            SortKey(OutboxCommand.created_at, descending=True),
            SortKey(OutboxCommand.id, descending=True),
        ),
        limit=max(1, min(limit, 100)),
        cursor=cursor,
        principal=f"account:{current.account.id}",
        resource="admin:runtime:outbox",
        filters=filters,
    )
    return {
        "items": [{
            "id": item.id,
            "topic": item.topic,
            "action": item.action,
            "aggregate_type": item.aggregate_type,
            "aggregate_id": item.aggregate_id,
            "status": item.status,
            "priority": item.priority,
            "attempt_count": item.attempt_count,
            "available_at": item.available_at.isoformat(),
            "lease_owner": item.dispatch_lease_owner,
            "lease_expires_at": (
                item.dispatch_lease_expires_at.isoformat()
                if item.dispatch_lease_expires_at else None
            ),
            "has_error": bool(item.last_error),
            "created_at": item.created_at.isoformat(),
            "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        } for item in rows],
        "next_cursor": next_cursor,
    }
