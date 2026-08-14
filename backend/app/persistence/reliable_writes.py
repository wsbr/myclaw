"""Single-transaction writers for execution events, audit, and outbox."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import utcnow, uuid7
from app.models.foundation import (
    ExecutionEvent,
    ExecutionEventRecipient,
    OutboxCommand,
    PlatformAuditEvent,
    PlatformAccount,
    AgentApiKey,
    UserSession,
)


class EventDedupeCollision(RuntimeError):
    pass


@dataclass(frozen=True)
class EventRecipient:
    recipient_type: str
    recipient_id: str


class ExecutionEventWriter:
    """Allocate a session sequence and append an immutable event."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(
        self,
        *,
        session_id: str,
        event_type: str,
        event_dedupe_key: str,
        actor_type: str,
        actor_id: str | None,
        entity_type: str,
        entity_id: str,
        visibility: str,
        summary: str,
        correlation_id: str,
        entity_version: int | None = None,
        payload: Mapping[str, Any] | None = None,
        transition_key: str | None = None,
        recipients: Iterable[EventRecipient] = (),
        private_owner: tuple[str, str] | None = None,
        caused_by_event_id: str | None = None,
        run_id: str | None = None,
        group_session_id: str | None = None,
        group_run_id: str | None = None,
    ) -> ExecutionEvent:
        recipient_list = list(recipients)
        if visibility == "direct" and not recipient_list:
            raise ValueError("direct events require at least one recipient")
        if visibility == "private" and private_owner is None:
            raise ValueError("private events require an owner")

        existing = await self.session.scalar(
            select(ExecutionEvent).where(
                ExecutionEvent.session_id == session_id,
                ExecutionEvent.event_dedupe_key == event_dedupe_key,
            )
        )
        if existing is not None:
            if (
                existing.event_type != event_type
                or existing.entity_type != entity_type
                or existing.entity_id != entity_id
            ):
                raise EventDedupeCollision(event_dedupe_key)
            return existing

        aggregate = await self.session.scalar(
            select(UserSession)
            .where(UserSession.id == session_id)
            # The aggregate may already be present in this AsyncSession from an
            # ownership check. After waiting for another transaction's row lock,
            # refresh it instead of reusing a stale identity-map sequence value.
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if aggregate is None:
            raise LookupError(f"session not found: {session_id}")

        sequence = aggregate.next_event_sequence
        aggregate.next_event_sequence += 1
        aggregate.version += 1
        owner_type, owner_id = private_owner or (None, None)
        event = ExecutionEvent(
            id=uuid7(),
            session_id=session_id,
            run_id=run_id,
            group_session_id=group_session_id,
            group_run_id=group_run_id,
            sequence=sequence,
            event_type=event_type,
            event_dedupe_key=event_dedupe_key,
            transition_key=transition_key,
            actor_type=actor_type,
            actor_id=actor_id,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_version=entity_version,
            visibility=visibility,
            private_owner_type=owner_type,
            private_owner_id=owner_id,
            summary=summary,
            payload=dict(payload or {}),
            caused_by_event_id=caused_by_event_id,
            correlation_id=correlation_id,
        )
        self.session.add(event)
        for recipient in recipient_list:
            self.session.add(
                ExecutionEventRecipient(
                    event_id=event.id,
                    recipient_type=recipient.recipient_type,
                    recipient_id=recipient.recipient_id,
                )
            )
        await self.session.flush()
        return event


class PlatformAuditWriter:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(
        self,
        *,
        category: str,
        action: str,
        actor_type: str,
        actor_id: str | None,
        actor_user_id: str | None,
        resource_type: str,
        resource_id: str,
        outcome: str,
        summary: str,
        dedupe_key: str,
        resource_version: int | None = None,
        severity: str = "info",
        changes: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        tenant_id: str | None = None,
    ) -> PlatformAuditEvent:
        existing = await self.session.scalar(
            select(PlatformAuditEvent).where(PlatformAuditEvent.dedupe_key == dedupe_key)
        )
        if existing is not None:
            if (
                existing.action != action
                or existing.resource_type != resource_type
                or existing.resource_id != resource_id
            ):
                raise EventDedupeCollision(dedupe_key)
            return existing
        if tenant_id is None and actor_id is not None:
            if actor_type == "account":
                tenant_id = await self.session.scalar(select(PlatformAccount.tenant_id).where(
                    PlatformAccount.id == actor_id,
                ))
            elif actor_type == "agent_api_key":
                tenant_id = await self.session.scalar(select(AgentApiKey.tenant_id).where(
                    AgentApiKey.id == actor_id,
                ))
        event = PlatformAuditEvent(
            id=uuid7(),
            tenant_id=tenant_id,
            occurred_at=utcnow(),
            category=category,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
            correlation_id=correlation_id,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_version=resource_version,
            outcome=outcome,
            severity=severity,
            summary=summary,
            changes=dict(changes or {}),
            event_metadata=dict(metadata or {}),
            dedupe_key=dedupe_key,
        )
        self.session.add(event)
        await self.session.flush()
        return event


class OutboxWriter:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self,
        *,
        topic: str,
        action: str,
        aggregate_type: str,
        aggregate_id: str,
        partition_key: str,
        dedupe_key: str,
        tenant_id: str | None = None,
        account_id: str | None = None,
        payload_ref: Mapping[str, Any] | None = None,
        priority: int = 100,
        available_at: datetime | None = None,
    ) -> OutboxCommand:
        if (tenant_id is None) != (account_id is None):
            raise ValueError(
                "Tenant-owned Outbox commands require both tenant_id and account_id"
            )
        existing = await self.session.scalar(
            select(OutboxCommand).where(
                OutboxCommand.topic == topic,
                OutboxCommand.dedupe_key == dedupe_key,
            )
        )
        if existing is not None:
            if (
                existing.action != action
                or existing.aggregate_type != aggregate_type
                or existing.aggregate_id != aggregate_id
                or existing.tenant_id != tenant_id
                or existing.account_id != account_id
            ):
                raise EventDedupeCollision(f"{topic}:{dedupe_key}")
            return existing
        command = OutboxCommand(
            id=uuid7(),
            tenant_id=tenant_id,
            account_id=account_id,
            topic=topic,
            action=action,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            partition_key=partition_key,
            dedupe_key=dedupe_key,
            payload_ref=dict(payload_ref or {}),
            priority=priority,
            available_at=available_at or utcnow(),
            status="pending",
        )
        self.session.add(command)
        await self.session.flush()
        return command


def tenant_command_error(
    command: OutboxCommand, *, tenant_id: str, account_id: str,
) -> str | None:
    """Return a fail-closed authority error for a tenant-owned command."""

    if command.tenant_id is None or command.account_id is None:
        return "tenant_authority_missing"
    if command.tenant_id != tenant_id or command.account_id != account_id:
        return "tenant_authority_mismatch"
    return None
