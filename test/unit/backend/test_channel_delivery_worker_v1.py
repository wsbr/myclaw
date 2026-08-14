import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.channel_plugins import (
    CHANNEL_PLUGINS, ChannelDeliveryResult, register_channel_plugin,
)
from app.database import Base
from app.models.channel_v1 import ChannelAccount, NotificationDelivery, NotificationDeliveryAttempt
from app.models.common import utcnow, uuid7
from app.models.foundation import ExecutionEvent, OutboxCommand, UserSession
from app.models.tenant_v1 import DEFAULT_TENANT_ID
from app.workers.channel_delivery_worker import ChannelDeliveryWorker

pytestmark = pytest.mark.unit


class FakeChannelPlugin:
    def __init__(self):
        self.requests = []

    async def deliver(self, request):
        self.requests.append(request)
        return ChannelDeliveryResult(
            status="succeeded", provider_message_key="provider-message-1",
            provider_request_key="provider-request-1",
        )


@pytest.mark.asyncio
async def test_delivery_worker_claims_attempt_and_settles_without_copying_event_payload(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session_id, event_id, account_id, delivery_id = uuid7(), uuid7(), uuid7(), uuid7()
    owner_account_id = uuid7()
    async with factory.begin() as db:
        db.add(UserSession(
            id=session_id, account_id=owner_account_id, user_id="u1",
            entry_agent_id=uuid7(), status="active",
            origin_type="channel", next_event_sequence=2,
        ))
        db.add(ExecutionEvent(
            id=event_id, session_id=session_id, sequence=1, event_type="run.completed",
            event_dedupe_key="trigger", actor_type="service", entity_type="run",
            entity_id=uuid7(), visibility="user", summary="Run completed safely",
            payload={"secret": "must-not-be-rendered"}, correlation_id="run-1",
        ))
        db.add(ChannelAccount(
            id=account_id, plugin_key="unit-test-channel", owner_account_id=owner_account_id,
            external_account_key="external-account", credential_ref="env://UNIT_TEST_CHANNEL_TOKEN",
            status="active", config={"safe": True},
        ))
        db.add(NotificationDelivery(
            id=delivery_id, session_id=session_id, user_id="u1", run_id=None,
            triggering_event_id=event_id, event_type="run_completed",
            template_key="channel.run_completed", template_version=1,
            channel_account_id=account_id, external_conversation_id="conversation-1",
            status="queued", dedupe_key=f"event:{event_id}:run_completed",
            provider_idempotency_key=f"notification:{event_id}", attempt_count=0,
        ))
        db.add(OutboxCommand(
            id=uuid7(), topic="runtime.delivery", action="deliver_notification",
            aggregate_type="notification_delivery", aggregate_id=delivery_id,
            partition_key=session_id, dedupe_key="deliver-1", payload_ref={"delivery_id": delivery_id},
            tenant_id=DEFAULT_TENANT_ID, account_id=owner_account_id,
            available_at=utcnow(), status="pending",
        ))

    import app.workers.channel_delivery_worker as worker_module
    monkeypatch.setattr(worker_module, "AsyncSessionLocal", factory)
    plugin = FakeChannelPlugin()
    register_channel_plugin("unit-test-channel", plugin)
    try:
        worker = ChannelDeliveryWorker("delivery-test")
        claim = await worker.claim_one()
        assert claim is not None
        assert "Run completed safely" in claim.text
        assert "must-not-be-rendered" not in claim.text
        await worker.deliver(claim)
    finally:
        CHANNEL_PLUGINS.pop("unit-test-channel", None)

    async with factory() as db:
        delivery = await db.get(NotificationDelivery, delivery_id)
        attempt = await db.scalar(select(NotificationDeliveryAttempt).where(
            NotificationDeliveryAttempt.delivery_id == delivery_id,
        ))
        command = await db.scalar(select(OutboxCommand).where(
            OutboxCommand.aggregate_id == delivery_id,
        ))
        assert delivery.status == "succeeded"
        assert delivery.provider_message_key == "provider-message-1"
        assert attempt.status == "succeeded"
        assert command.status == "completed"
        assert len(plugin.requests) == 1
    await engine.dispose()
