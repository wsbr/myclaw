"""Reliable consumer for Channel-origin NotificationDelivery commands."""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select

from app.core.channel_plugins import ChannelDeliveryRequest, ChannelDeliveryResult, get_channel_plugin
from app.database import AsyncSessionLocal
from app.models.channel_v1 import ChannelAccount, NotificationDelivery, NotificationDeliveryAttempt
from app.models.common import utcnow, uuid7
from app.models.foundation import ExecutionEvent, OutboxCommand, UserSession
from app.persistence.reliable_writes import (
    ExecutionEventWriter,
    OutboxWriter,
    tenant_command_error,
)


TERMINAL = {"succeeded", "failed", "cancelled", "outcome_unknown"}
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class DeliveryClaim:
    delivery_id: str
    attempt_id: str
    claim_token: str
    plugin_key: str
    external_conversation_id: str
    provider_idempotency_key: str
    text: str
    config: dict
    credential_ref: str


class ChannelDeliveryWorker:
    def __init__(self, worker_id: str | None = None):
        self.worker_id = worker_id or f"delivery:{socket.gethostname()}:{os.getpid()}"

    async def claim_one(self) -> DeliveryClaim | None:
        async with AsyncSessionLocal.begin() as db:
            now = utcnow()
            command = await db.scalar(
                select(OutboxCommand).where(
                    OutboxCommand.topic == "runtime.delivery",
                    OutboxCommand.action == "deliver_notification",
                    or_(
                        (OutboxCommand.status.in_(("pending", "retry_wait")))
                        & (OutboxCommand.available_at <= now),
                        (OutboxCommand.status == "claimed")
                        & (OutboxCommand.dispatch_lease_expires_at <= now),
                    ),
                ).order_by(OutboxCommand.priority, OutboxCommand.created_at)
                .with_for_update(skip_locked=True).limit(1)
            )
            if command is None:
                return None
            command.status = "claimed"
            command.attempt_count += 1
            command.dispatch_lease_owner = self.worker_id
            command.dispatch_lease_token = uuid7()
            command.dispatch_lease_expires_at = now + timedelta(seconds=30)

            delivery = await db.scalar(select(NotificationDelivery).where(
                NotificationDelivery.id == command.aggregate_id,
            ).with_for_update())
            if delivery is None:
                command.status = "dead"
                command.last_error = "NotificationDelivery does not exist"
                return None
            if delivery.status in TERMINAL:
                self._complete_command(command, now)
                return None
            if delivery.status == "delivering":
                await self._mark_unknown(
                    db, delivery, error_code="delivery_lease_expired",
                    detail="Delivery worker lease expired while provider outcome was unresolved",
                )
                self._complete_command(command, now)
                return None

            account = await db.scalar(select(ChannelAccount).where(
                ChannelAccount.id == delivery.channel_account_id,
                ChannelAccount.status == "active",
            ))
            session = await db.get(UserSession, delivery.session_id)
            triggering = await db.get(ExecutionEvent, delivery.triggering_event_id)
            if account is None or session is None or triggering is None:
                delivery.status = "failed"
                delivery.last_error_code = "channel_binding_unavailable"
                delivery.last_error_detail = "Channel account or triggering event is unavailable"
                delivery.version += 1
                await self._append_event(db, delivery, "notification.failed", "Channel notification failed")
                self._complete_command(command, now)
                return None
            authority_error = tenant_command_error(
                command,
                tenant_id=session.tenant_id,
                account_id=session.account_id,
            )
            if (
                authority_error
                or account.tenant_id != session.tenant_id
                or account.owner_account_id != session.account_id
            ):
                delivery.status = "failed"
                delivery.last_error_code = authority_error or "delivery_tenant_mismatch"
                delivery.last_error_detail = "Channel delivery authority envelope is invalid"
                delivery.version += 1
                command.status = "dead"
                command.last_error = delivery.last_error_code
                command.dispatch_lease_owner = None
                command.dispatch_lease_token = None
                command.dispatch_lease_expires_at = None
                return None

            claim_token = uuid7()
            delivery.attempt_count += 1
            delivery.status = "delivering"
            delivery.next_attempt_at = None
            delivery.version += 1
            attempt = NotificationDeliveryAttempt(
                id=uuid7(), delivery_id=delivery.id, attempt_no=delivery.attempt_count,
                status="delivering", claim_token=claim_token,
                provider_request_key=delivery.provider_idempotency_key,
                request_metadata={"template_key": delivery.template_key,
                                  "template_version": delivery.template_version},
            )
            db.add(attempt)
            await self._append_event(
                db, delivery, "notification.attempt_started", "Channel notification attempt started",
                visibility="private", private_owner=("notification_delivery", delivery.id),
                suffix=f"attempt:{attempt.attempt_no}:started",
            )
            # Outbox acceptance is complete once the independent Delivery
            # Attempt lease exists; provider I/O happens outside this transaction.
            self._complete_command(command, now)
            text = (
                f"{triggering.summary}\n"
                f"Event: {delivery.event_type}"
                + (f"\nRun: {delivery.run_id}" if delivery.run_id else "")
            )
            return DeliveryClaim(
                delivery_id=delivery.id, attempt_id=attempt.id, claim_token=claim_token,
                plugin_key=account.plugin_key,
                external_conversation_id=delivery.external_conversation_id,
                provider_idempotency_key=delivery.provider_idempotency_key,
                text=text, config=dict(account.config or {}), credential_ref=account.credential_ref,
            )

    async def deliver(self, claim: DeliveryClaim) -> None:
        plugin = get_channel_plugin(claim.plugin_key)
        if plugin is None:
            result = ChannelDeliveryResult(
                status="failed", error_code="channel_plugin_unavailable",
                error_detail=f"Channel plugin is not registered: {claim.plugin_key}", retryable=False,
            )
        else:
            credential = None
            if claim.credential_ref.startswith("env://"):
                credential = os.getenv(claim.credential_ref[6:])
            try:
                result = await plugin.deliver(ChannelDeliveryRequest(
                    external_conversation_id=claim.external_conversation_id,
                    idempotency_key=claim.provider_idempotency_key,
                    text=claim.text, config=claim.config, credential=credential,
                ))
            except Exception as error:
                result = ChannelDeliveryResult(
                    status="failed", error_code="channel_provider_error",
                    error_detail=str(error)[:2000], retryable=True,
                )
        await self.settle(claim, result)

    async def settle(self, claim: DeliveryClaim, result: ChannelDeliveryResult) -> None:
        async with AsyncSessionLocal.begin() as db:
            delivery = await db.scalar(select(NotificationDelivery).where(
                NotificationDelivery.id == claim.delivery_id,
            ).with_for_update())
            attempt = await db.scalar(select(NotificationDeliveryAttempt).where(
                NotificationDeliveryAttempt.id == claim.attempt_id,
                NotificationDeliveryAttempt.claim_token == claim.claim_token,
            ).with_for_update())
            if delivery is None or attempt is None or delivery.status != "delivering" or attempt.status != "delivering":
                return
            attempt.provider_message_key = result.provider_message_key
            attempt.response_metadata = {
                "provider_request_key": result.provider_request_key,
                "provider_message_key": result.provider_message_key,
            }
            if result.status == "succeeded":
                attempt.status = "succeeded"
                delivery.status = "succeeded"
                delivery.provider_message_key = result.provider_message_key
                delivery.delivered_at = utcnow()
                delivery.last_error_code = None
                delivery.last_error_detail = None
                delivery.version += 1
                await self._append_event(db, delivery, "notification.succeeded", "Channel notification delivered")
                return
            attempt.error_code = result.error_code or "channel_delivery_failed"
            attempt.error_detail = (result.error_detail or "Channel delivery failed")[:4000]
            attempt.retryable = result.retryable
            if result.status == "outcome_unknown":
                attempt.status = "outcome_unknown"
                await self._mark_unknown(db, delivery, error_code=attempt.error_code, detail=attempt.error_detail)
                return
            attempt.status = "failed"
            delivery.last_error_code = attempt.error_code
            delivery.last_error_detail = attempt.error_detail
            delivery.version += 1
            if result.retryable and delivery.attempt_count < MAX_ATTEMPTS:
                delay = 2 ** delivery.attempt_count
                delivery.status = "retry_wait"
                delivery.next_attempt_at = utcnow() + timedelta(seconds=delay)
                scheduled = await self._append_event(
                    db, delivery, "notification.retry_scheduled", "Channel notification retry scheduled",
                    visibility="private", private_owner=("notification_delivery", delivery.id),
                    suffix=f"attempt:{delivery.attempt_count}:retry",
                )
                account = await db.get(ChannelAccount, delivery.channel_account_id)
                if account is None:
                    delivery.status = "failed"
                    delivery.last_error_code = "channel_account_missing"
                    delivery.last_error_detail = "Channel account was removed before retry"
                    return
                await OutboxWriter(db).enqueue(
                    topic="runtime.delivery", action="deliver_notification",
                    aggregate_type="notification_delivery", aggregate_id=delivery.id,
                    partition_key=delivery.session_id,
                    dedupe_key=f"event:{scheduled.id}:deliver",
                    tenant_id=account.tenant_id, account_id=account.owner_account_id,
                    payload_ref={"delivery_id": delivery.id}, available_at=delivery.next_attempt_at,
                )
            else:
                delivery.status = "failed"
                await self._append_event(db, delivery, "notification.failed", "Channel notification failed")

    async def recover_one(self, stale_seconds: int = 60) -> bool:
        async with AsyncSessionLocal.begin() as db:
            cutoff = utcnow() - timedelta(seconds=stale_seconds)
            delivery = await db.scalar(select(NotificationDelivery).where(
                NotificationDelivery.status == "delivering",
                NotificationDelivery.updated_at <= cutoff,
            ).order_by(NotificationDelivery.updated_at).with_for_update(skip_locked=True).limit(1))
            if delivery is None:
                return False
            attempt = await db.scalar(select(NotificationDeliveryAttempt).where(
                NotificationDeliveryAttempt.delivery_id == delivery.id,
                NotificationDeliveryAttempt.status == "delivering",
            ).order_by(NotificationDeliveryAttempt.attempt_no.desc()).with_for_update().limit(1))
            if attempt is not None:
                attempt.status = "outcome_unknown"
                attempt.error_code = "delivery_attempt_abandoned"
                attempt.error_detail = "Worker stopped after provider call may have started"
            await self._mark_unknown(
                db, delivery, error_code="delivery_attempt_abandoned",
                detail="Worker stopped after provider call may have started",
            )
            return True

    async def run_forever(self, poll_seconds: float = 0.5) -> None:
        while True:
            if await self.recover_one():
                continue
            claim = await self.claim_one()
            if claim is None:
                await asyncio.sleep(poll_seconds)
                continue
            await self.deliver(claim)

    async def _mark_unknown(self, db, delivery: NotificationDelivery, *, error_code: str, detail: str) -> None:
        delivery.status = "outcome_unknown"
        delivery.last_error_code = error_code
        delivery.last_error_detail = detail[:4000]
        delivery.version += 1
        await self._append_event(db, delivery, "notification.outcome_unknown", "Channel notification outcome is unknown")

    async def _append_event(
        self, db, delivery: NotificationDelivery, event_type: str, summary: str, *,
        visibility: str = "user", private_owner: tuple[str, str] | None = None,
        suffix: str | None = None,
    ):
        return await ExecutionEventWriter(db).append(
            session_id=delivery.session_id, run_id=delivery.run_id, group_run_id=delivery.group_run_id,
            event_type=event_type,
            event_dedupe_key=f"transition:notification:{delivery.id}:{suffix or event_type}",
            actor_type="service", actor_id=self.worker_id,
            entity_type="notification_delivery", entity_id=delivery.id,
            entity_version=delivery.version, visibility=visibility, private_owner=private_owner,
            summary=summary, payload={"delivery_id": delivery.id, "status": delivery.status},
            correlation_id=delivery.run_id or delivery.session_id,
            caused_by_event_id=delivery.triggering_event_id,
        )

    @staticmethod
    def _complete_command(command: OutboxCommand, now) -> None:
        command.status = "completed"
        command.completed_at = now
        command.dispatch_lease_owner = None
        command.dispatch_lease_token = None
        command.dispatch_lease_expires_at = None


async def main() -> None:
    await ChannelDeliveryWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(main())
