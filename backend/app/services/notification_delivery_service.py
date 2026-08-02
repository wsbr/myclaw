"""Create Channel-origin deliveries without manufacturing Web/API notifications."""

from sqlalchemy import select

from app.models.channel_v1 import ChannelAccount, ChannelConversationBinding, NotificationDelivery
from app.models.common import uuid7
from app.models.foundation import UserSession
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter


async def create_notification_delivery_if_channel(
    db, *, session_id: str, triggering_event_id: str, event_type: str,
    run_id: str | None = None, group_run_id: str | None = None,
) -> NotificationDelivery | None:
    session = await db.get(UserSession, session_id)
    if (session is None or session.origin_type != "channel"
            or not session.origin_channel_account_id or not session.origin_external_conversation_id):
        return None
    if len(session.origin_external_conversation_id) > 300:
        return None
    account = await db.scalar(select(ChannelAccount).where(
        ChannelAccount.id == session.origin_channel_account_id,
        ChannelAccount.status == "active",
    ))
    binding = await db.scalar(select(ChannelConversationBinding).where(
        ChannelConversationBinding.channel_account_id == session.origin_channel_account_id,
        ChannelConversationBinding.external_conversation_id == session.origin_external_conversation_id,
        ChannelConversationBinding.session_id == session.id,
        ChannelConversationBinding.user_id == session.user_id,
        ChannelConversationBinding.status == "active",
    ))
    if account is None or binding is None:
        return None
    dedupe_key = f"event:{triggering_event_id}:{event_type}"
    existing = await db.scalar(select(NotificationDelivery).where(
        NotificationDelivery.session_id == session.id,
        NotificationDelivery.dedupe_key == dedupe_key,
    ))
    if existing is not None:
        return existing
    delivery = NotificationDelivery(
        id=uuid7(), session_id=session.id, user_id=session.user_id,
        run_id=run_id, group_run_id=group_run_id,
        triggering_event_id=triggering_event_id, event_type=event_type,
        template_key=f"channel.{event_type}", template_version=1,
        channel_account_id=account.id,
        external_conversation_id=binding.external_conversation_id,
        status="queued", dedupe_key=dedupe_key,
        provider_idempotency_key=f"notification:{triggering_event_id}", attempt_count=0,
    )
    db.add(delivery); await db.flush()
    created = await ExecutionEventWriter(db).append(
        session_id=session.id, run_id=run_id,
        group_run_id=group_run_id,
        event_type="notification.created",
        event_dedupe_key=f"transition:notification:{delivery.id}:created",
        actor_type="service", actor_id="notification-router",
        entity_type="notification_delivery", entity_id=delivery.id,
        entity_version=delivery.version, visibility="user",
        summary="Channel notification queued",
        payload={"delivery_id": delivery.id, "event_type": event_type},
        correlation_id=run_id or session.id, caused_by_event_id=triggering_event_id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.delivery", action="deliver_notification",
        aggregate_type="notification_delivery", aggregate_id=delivery.id,
        partition_key=session.id, dedupe_key=f"event:{created.id}:deliver",
        payload_ref={"delivery_id": delivery.id},
    )
    return delivery


def notification_view(delivery: NotificationDelivery, plugin_key: str | None = None) -> dict:
    return {
        "id": delivery.id, "session_id": delivery.session_id, "run_id": delivery.run_id,
        "group_run_id": delivery.group_run_id, "event_type": delivery.event_type,
        "channel_label": plugin_key or "channel", "status": delivery.status,
        "attempt_count": delivery.attempt_count, "last_error_code": delivery.last_error_code,
        "next_attempt_at": delivery.next_attempt_at.isoformat() if delivery.next_attempt_at else None,
        "delivered_at": delivery.delivered_at.isoformat() if delivery.delivered_at else None,
        "created_at": delivery.created_at.isoformat(), "version": delivery.version,
    }
