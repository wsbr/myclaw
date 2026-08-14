"""Usage/cost ledger queries and immutable price administration."""

from __future__ import annotations

from datetime import timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.registries_v1 import idem
from app.api.runs import _owned_run
from app.database import get_db
from app.deps import CurrentAccount, SessionActor, get_current_admin, get_session_actor
from app.models.usage_v1 import UsageLedgerEntry, UsagePriceSnapshot
from app.persistence.reliable_writes import PlatformAuditWriter
from app.schemas.usage_v1 import UsageAdjustmentCreate, UsagePriceSnapshotCreate
from app.services.cursor_service import SortKey, paginate_scalars
from app.services.usage_ledger_service import (
    create_price_snapshot,
    create_usage_adjustment,
    price_snapshot_view,
    usage_entry_view,
    usage_rollup,
)


router = APIRouter(tags=["usage-ledger"])


def _naive_utc(value):
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@router.post("/admin/usage/prices", status_code=201)
async def add_usage_price(
    body: UsagePriceSnapshotCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_admin),
):
    request = body.model_dump(mode="json")
    record, replay = await idem(
        db, current, "create_usage_price_snapshot", idempotency_key, request,
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    try:
        item = await create_price_snapshot(
            db,
            subject_type=body.subject_type,
            provider=body.provider,
            subject_key=body.subject_key,
            metric=body.metric,
            unit=body.unit,
            currency=body.currency,
            price_per_unit=body.price_per_unit,
            price_status=body.price_status,
            effective_from=_naive_utc(body.effective_from),
            effective_to=(
                _naive_utc(body.effective_to) if body.effective_to else None
            ),
            source_label=body.source_label,
            actor_account_id=current.account.id,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": str(error)},
        ) from error
    response = price_snapshot_view(item)
    await PlatformAuditWriter(db).append(
        category="registry", action="usage_price.created",
        actor_type="account", actor_id=current.account.id,
        actor_user_id=current.account.user_id,
        resource_type="usage_price_snapshot", resource_id=item.id,
        outcome="succeeded", summary="Immutable usage price snapshot created",
        changes={
            "subject_type": item.subject_type,
            "provider": item.provider,
            "subject_key": item.subject_key,
            "metric": item.metric,
            "currency": item.currency,
            "price_status": item.price_status,
            "content_hash": item.content_hash,
        },
        dedupe_key=f"audit:create_usage_price_snapshot:{record.id}",
    )
    record.resource_type = "usage_price_snapshot"
    record.resource_id = item.id
    record.http_status = 201
    record.response_snapshot = response
    await db.commit()
    return response


@router.get("/admin/usage/prices")
async def list_usage_prices(
    subject_type: str | None = None,
    provider: str | None = None,
    subject_key: str | None = None,
    metric: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_admin),
):
    query = select(UsagePriceSnapshot)
    for column, value in (
        (UsagePriceSnapshot.subject_type, subject_type),
        (UsagePriceSnapshot.provider, provider),
        (UsagePriceSnapshot.subject_key, subject_key),
        (UsagePriceSnapshot.metric, metric),
    ):
        if value:
            query = query.where(column == value)
    filters = {
        "subject_type": subject_type, "provider": provider,
        "subject_key": subject_key, "metric": metric,
    }
    rows, next_cursor = await paginate_scalars(
        db, query,
        sort=(
            SortKey(UsagePriceSnapshot.effective_from, descending=True),
            SortKey(UsagePriceSnapshot.id, descending=True),
        ),
        limit=max(1, min(limit, 100)), cursor=cursor,
        principal=f"account:{current.account.id}",
        resource="admin:usage:prices", filters=filters,
    )
    return {
        "items": [price_snapshot_view(item) for item in rows],
        "next_cursor": next_cursor,
    }


@router.post("/admin/usage/adjustments", status_code=201)
async def add_usage_adjustment(
    body: UsageAdjustmentCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_admin),
):
    record, replay = await idem(
        db, current, "create_usage_adjustment", idempotency_key,
        body.model_dump(mode="json"),
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    original = await db.scalar(select(UsageLedgerEntry).where(
        UsageLedgerEntry.id == body.original_entry_id,
        UsageLedgerEntry.tenant_id == current.tenant_id,
    ))
    if original is None:
        raise HTTPException(status_code=404, detail="Usage entry not found")
    item = await create_usage_adjustment(
        db, original=original, quantity_delta=body.quantity_delta,
        reason=body.reason, operation_key=f"usage-adjustment:{record.id}",
    )
    response = usage_entry_view(item)
    await PlatformAuditWriter(db).append(
        category="maintenance", action="usage.adjusted",
        actor_type="account", actor_id=current.account.id,
        actor_user_id=current.account.user_id,
        resource_type="usage_ledger_entry", resource_id=item.id,
        outcome="succeeded", summary="Usage correction appended",
        changes={
            "adjusts_entry_id": original.id,
            "metric": item.metric,
            "quantity_delta": str(item.quantity),
            "reason": body.reason,
        },
        dedupe_key=f"audit:create_usage_adjustment:{record.id}",
    )
    record.resource_type = "usage_ledger_entry"
    record.resource_id = item.id
    record.http_status = 201
    record.response_snapshot = response
    await db.commit()
    return response


def _entry_query(
    *, tenant_id: str, run_id: str | None, account_id: str | None,
    agent_version_id: str | None, model_version_id: str | None,
):
    query = select(UsageLedgerEntry).where(
        UsageLedgerEntry.tenant_id == tenant_id,
    )
    for column, value in (
        (UsageLedgerEntry.run_id, run_id),
        (UsageLedgerEntry.account_id, account_id),
        (UsageLedgerEntry.agent_version_id, agent_version_id),
        (UsageLedgerEntry.model_version_id, model_version_id),
    ):
        if value:
            query = query.where(column == value)
    return query


@router.get("/admin/usage/entries")
async def list_usage_entries(
    run_id: str | None = None,
    account_id: str | None = None,
    agent_version_id: str | None = None,
    model_version_id: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_admin),
):
    filters = {
        "run_id": run_id, "account_id": account_id,
        "agent_version_id": agent_version_id,
        "model_version_id": model_version_id,
    }
    rows, next_cursor = await paginate_scalars(
        db,
        _entry_query(tenant_id=current.tenant_id, **filters),
        sort=(
            SortKey(UsageLedgerEntry.created_at, descending=True),
            SortKey(UsageLedgerEntry.id, descending=True),
        ),
        limit=max(1, min(limit, 100)), cursor=cursor,
        principal=f"account:{current.account.id}",
        resource="admin:usage:entries", filters=filters,
    )
    return {
        "items": [usage_entry_view(item) for item in rows],
        "next_cursor": next_cursor,
    }


@router.get("/admin/usage/rollups")
async def get_usage_rollups(
    group_by: str = "run_id",
    run_id: str | None = None,
    account_id: str | None = None,
    agent_version_id: str | None = None,
    model_version_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_admin),
):
    allowed = {
        "run_id", "account_id", "group_run_id", "agent_version_id", "model_version_id",
    }
    if group_by not in allowed:
        raise HTTPException(status_code=422, detail={"code": "invalid_usage_group_by"})
    rows = list(await db.scalars(_entry_query(
        tenant_id=current.tenant_id,
        run_id=run_id, account_id=account_id,
        agent_version_id=agent_version_id, model_version_id=model_version_id,
    ).order_by(UsageLedgerEntry.created_at, UsageLedgerEntry.id)))
    return {"group_by": group_by, "items": usage_rollup(rows, group_by=group_by)}


@router.get("/runs/{run_id}/usage")
async def get_owned_run_usage(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    await _owned_run(db, run_id, actor)
    rows = list(await db.scalars(select(UsageLedgerEntry).where(
        UsageLedgerEntry.tenant_id == actor.tenant_id,
        UsageLedgerEntry.run_id == run_id,
    ).order_by(UsageLedgerEntry.created_at, UsageLedgerEntry.id)))
    return {
        "run_id": run_id,
        "entries": [usage_entry_view(item) for item in rows],
        "rollup": usage_rollup(rows, group_by="run_id"),
    }
