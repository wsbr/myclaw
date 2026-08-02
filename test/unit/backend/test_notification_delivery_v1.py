"""Only an active Channel-origin binding creates NotificationDelivery state."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.channel_v1 import ChannelAccount, ChannelConversationBinding, NotificationDelivery
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    ExecutionEvent, ExecutionEventRecipient, OutboxCommand, PlatformAccount,
    UserIdentity, UserSession,
)
from app.persistence.reliable_writes import ExecutionEventWriter
from app.services.notification_delivery_service import create_notification_delivery_if_channel

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_delivery_requires_channel_origin_and_active_binding():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [UserIdentity.__table__, PlatformAccount.__table__, ChannelAccount.__table__,
        UserSession.__table__, ChannelConversationBinding.__table__, ExecutionEvent.__table__,
        ExecutionEventRecipient.__table__, NotificationDelivery.__table__, OutboxCommand.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, channel_id, channel_session, web_session = (uuid7() for _ in range(4))
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="u1", status="active"),
            PlatformAccount(id=account_id, user_id="u1", display_name="U", status="active"),
            ChannelAccount(id=channel_id, plugin_key="test-channel", owner_account_id=account_id,
                external_account_key="bot-1", credential_ref="env://TEST_CHANNEL", status="active", config={}),
            UserSession(id=channel_session, user_id="u1", entry_agent_id=uuid7(), status="active",
                origin_type="channel", origin_channel_account_id=channel_id,
                origin_external_conversation_id="conversation-1", next_event_sequence=1),
            UserSession(id=web_session, user_id="u1", entry_agent_id=uuid7(), status="active",
                origin_type="web", next_event_sequence=1),
            ChannelConversationBinding(id=uuid7(), channel_account_id=channel_id,
                external_conversation_id="conversation-1", session_id=channel_session,
                user_id="u1", status="active"),
        ])
    async with factory.begin() as db:
        channel_event = await ExecutionEventWriter(db).append(session_id=channel_session,
            event_type="clarification.required", event_dedupe_key="channel-required",
            actor_type="agent", actor_id="a1", entity_type="clarification", entity_id=uuid7(),
            visibility="user", summary="Clarification required", correlation_id="r1")
        web_event = await ExecutionEventWriter(db).append(session_id=web_session,
            event_type="clarification.required", event_dedupe_key="web-required",
            actor_type="agent", actor_id="a1", entity_type="clarification", entity_id=uuid7(),
            visibility="user", summary="Clarification required", correlation_id="r2")
        delivery = await create_notification_delivery_if_channel(db, session_id=channel_session,
            triggering_event_id=channel_event.id, event_type="clarification_required")
        assert delivery is not None and delivery.status == "queued"
        assert await create_notification_delivery_if_channel(db, session_id=web_session,
            triggering_event_id=web_event.id, event_type="clarification_required") is None
        replay = await create_notification_delivery_if_channel(db, session_id=channel_session,
            triggering_event_id=channel_event.id, event_type="clarification_required")
        assert replay.id == delivery.id
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(NotificationDelivery)) == 1
        command = await db.scalar(select(OutboxCommand).where(OutboxCommand.topic == "runtime.delivery"))
        assert command.action == "deliver_notification"
    await engine.dispose()
