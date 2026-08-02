from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.sessions import _owned_session
from app.database import get_db
from app.deps import CurrentAccount, SessionActor, get_current_account, get_session_actor
from app.models.channel_v1 import ChannelAccount, NotificationDelivery
from app.services.notification_delivery_service import notification_view
from app.services.cursor_service import SortKey, paginate_scalars

router = APIRouter(tags=["notification-deliveries"])


@router.get("/sessions/{session_id}/notification-deliveries")
async def list_session_deliveries(
    session_id: str, status: str | None = None, event_type: str | None = None,
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    await _owned_session(db, session_id, actor)
    statement = select(NotificationDelivery).where(NotificationDelivery.session_id == session_id)
    if status: statement = statement.where(NotificationDelivery.status == status)
    if event_type: statement = statement.where(NotificationDelivery.event_type == event_type)
    rows, next_cursor = await paginate_scalars(
        db, statement,
        sort=(SortKey(NotificationDelivery.created_at), SortKey(NotificationDelivery.id)),
        limit=limit, cursor=cursor,
        principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=f"session:{session_id}:notification-deliveries",
        filters={"status": status, "event_type": event_type},
    )
    items = []
    for delivery in rows:
        account = await db.get(ChannelAccount, delivery.channel_account_id)
        items.append(notification_view(delivery, account.plugin_key))
    return {"items": items, "next_cursor": next_cursor}


@router.get("/notification-deliveries/{delivery_id}")
async def get_delivery(delivery_id: str, db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor)):
    row = (await db.execute(select(NotificationDelivery, ChannelAccount).join(
        ChannelAccount, ChannelAccount.id == NotificationDelivery.channel_account_id,
    ).where(NotificationDelivery.id == delivery_id))).first()
    if row is None: raise HTTPException(status_code=404, detail="NotificationDelivery not found")
    await _owned_session(db, row[0].session_id, actor)
    return notification_view(row[0], row[1].plugin_key)


@router.get("/admin/notification-deliveries")
async def admin_deliveries(
    status: str | None = None, channel_account_id: str | None = None,
    from_at: datetime | None = Query(default=None, alias="from"),
    to_at: datetime | None = Query(default=None, alias="to"),
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account)):
    if not current.has_any_role("admin"): raise HTTPException(status_code=403, detail="Admin required")
    statement = select(NotificationDelivery)
    if status: statement = statement.where(NotificationDelivery.status == status)
    if channel_account_id: statement = statement.where(NotificationDelivery.channel_account_id == channel_account_id)
    if from_at: statement = statement.where(NotificationDelivery.created_at >= from_at)
    if to_at: statement = statement.where(NotificationDelivery.created_at <= to_at)
    filters = {"status": status, "channel_account_id": channel_account_id,
               "from": from_at, "to": to_at}
    rows, next_cursor = await paginate_scalars(
        db, statement,
        sort=(SortKey(NotificationDelivery.created_at), SortKey(NotificationDelivery.id)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource="admin:notification-deliveries", filters=filters,
    )
    items = []
    for delivery in rows:
        account = await db.get(ChannelAccount, delivery.channel_account_id)
        items.append(notification_view(delivery, account.plugin_key))
    return {"items": items, "next_cursor": next_cursor}
