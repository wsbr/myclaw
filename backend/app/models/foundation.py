"""Phase-one identity, reliable event, audit, and outbox entities.

These tables intentionally coexist with the legacy tables during the staged
migration. Runtime code must use these singular, phase-one table names.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import CreatedAtMixin, TimestampMixin, VersionMixin, uuid7


class UserIdentity(Base, CreatedAtMixin):
    __tablename__ = "user_identity"
    __table_args__ = (
        CheckConstraint("status IN ('active','orphaned')", name="ck_user_identity_status"),
    )

    user_id: Mapped[str] = mapped_column(String(191), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)


class PlatformAccount(Base, TimestampMixin, VersionMixin):
    __tablename__ = "platform_account"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','discarded','disabled')",
            name="ck_platform_account_status",
        ),
        Index("ix_platform_account_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    user_id: Mapped[str] = mapped_column(
        String(191), ForeignKey("user_identity.user_id"), unique=True, nullable=False
    )
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    auth_revision: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)


class PlatformAccountRole(Base):
    __tablename__ = "platform_account_role"
    __table_args__ = (
        CheckConstraint("role_key IN ('user','builder','admin')", name="ck_account_role_key"),
    )

    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), primary_key=True
    )
    role_key: Mapped[str] = mapped_column(String(20), primary_key=True)
    granted_by_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform_account.id")
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class IdempotencyRecord(Base, CreatedAtMixin):
    __tablename__ = "idempotency_record"
    __table_args__ = (
        UniqueConstraint(
            "principal_type",
            "principal_id",
            "operation",
            "idempotency_key",
            name="uq_idempotency_principal_operation_key",
        ),
        Index("ix_idempotency_expiry", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(191), nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(36))
    http_status: Mapped[int | None] = mapped_column(Integer)
    response_snapshot: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class AgentApiKey(Base, TimestampMixin, VersionMixin):
    __tablename__ = "agent_api_key"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','revoked','expired')", name="ck_agent_api_key_status"
        ),
        Index("ix_agent_api_key_agent_status", "agent_id", "status"),
        Index("ix_agent_api_key_expiry", "expires_at", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    caller_label: Mapped[str] = mapped_column(String(200), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_by_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    rotated_from_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_api_key.id")
    )


class UserSession(Base, TimestampMixin, VersionMixin):
    """Session shell introduced early so reliable events have a real aggregate."""

    __tablename__ = "user_session"
    __table_args__ = (
        CheckConstraint("status IN ('active','closed','archived')", name="ck_user_session_status"),
        CheckConstraint("origin_type IN ('web','api','channel')", name="ck_user_session_origin"),
        CheckConstraint(
            "interaction_mode IN ('chat','plan')",
            name="ck_user_session_interaction_mode",
        ),
        UniqueConstraint("id", "user_id", name="uq_user_session_id_user"),
        Index("ix_user_session_owner_status", "user_id", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    user_id: Mapped[str] = mapped_column(
        String(191), ForeignKey("user_identity.user_id"), nullable=False
    )
    # account_id is the v2 authority. user_id remains during the UAT
    # expand/contract period so existing sessions stay readable.
    account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform_account.id")
    )
    entry_agent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    origin_type: Mapped[str] = mapped_column(String(20), default="web", nullable=False)
    origin_api_key_id: Mapped[str | None] = mapped_column(String(36))
    origin_channel_account_id: Mapped[str | None] = mapped_column(String(36))
    origin_external_conversation_id: Mapped[str | None] = mapped_column(String(500))
    next_event_sequence: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    permission_mode: Mapped[str] = mapped_column(
        String(32), default="direct", nullable=False
    )
    permission_revision: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    # Group is an associated collaboration resource, not a Session mode.  This
    # field records the user's current Chat/Plan interaction choice while
    # permission_mode remains the independent machine authorization boundary.
    interaction_mode: Mapped[str] = mapped_column(
        String(16), default="chat", nullable=False
    )
    interaction_revision: Mapped[int] = mapped_column(
        BigInteger, default=1, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class ExecutionEvent(Base, CreatedAtMixin):
    __tablename__ = "execution_event"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_execution_event_sequence"),
        UniqueConstraint(
            "session_id", "event_dedupe_key", name="uq_execution_event_dedupe"
        ),
        CheckConstraint(
            "visibility IN ('user','group','direct','private')",
            name="ck_execution_event_visibility",
        ),
        CheckConstraint(
            "visibility != 'private' OR (private_owner_type IS NOT NULL AND private_owner_id IS NOT NULL)",
            name="ck_private_event_owner",
        ),
        Index("ix_execution_event_session_scan", "session_id", "sequence"),
        Index("ix_execution_event_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_execution_event_transition", "transition_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_session.id"), nullable=False
    )
    run_id: Mapped[str | None] = mapped_column(String(36))
    group_session_id: Mapped[str | None] = mapped_column(String(36))
    group_run_id: Mapped[str | None] = mapped_column(String(36))
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    event_dedupe_key: Mapped[str] = mapped_column(String(191), nullable=False)
    transition_key: Mapped[str | None] = mapped_column(String(191))
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(191))
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entity_version: Mapped[int | None] = mapped_column(BigInteger)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)
    private_owner_type: Mapped[str | None] = mapped_column(String(32))
    private_owner_id: Mapped[str | None] = mapped_column(String(36))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    caused_by_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("execution_event.id")
    )
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)


class ExecutionEventRecipient(Base):
    __tablename__ = "execution_event_recipient"
    __table_args__ = (
        CheckConstraint(
            "recipient_type IN ('user_identity','group_membership','agent')",
            name="ck_execution_event_recipient_type",
        ),
        Index("ix_execution_event_recipient_lookup", "recipient_type", "recipient_id", "event_id"),
    )

    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("execution_event.id", ondelete="CASCADE"), primary_key=True
    )
    recipient_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    recipient_id: Mapped[str] = mapped_column(String(191), primary_key=True)


class PlatformAuditEvent(Base, CreatedAtMixin):
    __tablename__ = "platform_audit_event"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_platform_audit_dedupe"),
        CheckConstraint("outcome IN ('succeeded','failed')", name="ck_platform_audit_outcome"),
        CheckConstraint("severity IN ('info','warning','critical')", name="ck_platform_audit_severity"),
        Index("ix_platform_audit_resource", "resource_type", "resource_id", "occurred_at"),
        Index("ix_platform_audit_actor", "actor_type", "actor_id", "occurred_at"),
        Index("ix_platform_audit_filter", "category", "action", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(191))
    actor_user_id: Mapped[str | None] = mapped_column(String(191))
    request_id: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(191), nullable=False)
    resource_version: Mapped[int | None] = mapped_column(BigInteger)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="info", nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    changes: Mapped[dict | None] = mapped_column(JSON)
    event_metadata: Mapped[dict | None] = mapped_column("metadata", JSON)
    source_ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent_hash: Mapped[str | None] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(191), nullable=False)


class OutboxCommand(Base, TimestampMixin):
    __tablename__ = "outbox_command"
    __table_args__ = (
        UniqueConstraint("topic", "dedupe_key", name="uq_outbox_topic_dedupe"),
        CheckConstraint(
            "topic IN ('runtime.control','runtime.runner','runtime.tool','runtime.sandbox','runtime.delivery','runtime.maintenance')",
            name="ck_outbox_topic",
        ),
        CheckConstraint(
            "status IN ('pending','claimed','retry_wait','completed','dead')",
            name="ck_outbox_status",
        ),
        Index(
            "ix_outbox_claim",
            "topic",
            "status",
            "available_at",
            "priority",
            "created_at",
        ),
        Index("ix_outbox_reclaim", "status", "dispatch_lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    topic: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(191), nullable=False)
    partition_key: Mapped[str] = mapped_column(String(191), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(191), nullable=False)
    payload_ref: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dispatch_lease_owner: Mapped[str | None] = mapped_column(String(191))
    dispatch_lease_token: Mapped[str | None] = mapped_column(String(36))
    dispatch_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_error: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
