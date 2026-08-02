"""Channel bindings and reliable NotificationDelivery state."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, uuid7


class ChannelAccount(Base, TimestampMixin, VersionMixin):
    __tablename__ = "channel_account"
    __table_args__ = (
        CheckConstraint("status IN ('active','disabled','revoked')", name="ck_channel_account_status"),
        UniqueConstraint("plugin_key", "external_account_key", name="uq_channel_account_external"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    plugin_key: Mapped[str] = mapped_column(String(191), nullable=False)
    owner_account_id: Mapped[str] = mapped_column(String(36), ForeignKey("platform_account.id"), nullable=False)
    external_account_key: Mapped[str] = mapped_column(String(500), nullable=False)
    credential_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AgentChannelBinding(Base, TimestampMixin, VersionMixin):
    __tablename__ = "agent_channel_binding"
    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="ck_agent_channel_binding_status"),
        UniqueConstraint("channel_account_id", "external_route_key", name="uq_agent_channel_route"),
    )
    channel_account_id: Mapped[str] = mapped_column(String(36), ForeignKey("channel_account.id"), primary_key=True)
    external_route_key: Mapped[str] = mapped_column(String(500), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)


class ChannelIdentityBinding(Base):
    __tablename__ = "channel_identity_binding"
    channel_account_id: Mapped[str] = mapped_column(String(36), ForeignKey("channel_account.id"), primary_key=True)
    external_user_id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class ChannelConversationBinding(Base, TimestampMixin, VersionMixin):
    __tablename__ = "channel_conversation_binding"
    __table_args__ = (
        CheckConstraint("status IN ('active','closed')", name="ck_channel_conversation_binding_status"),
        UniqueConstraint("channel_account_id", "external_conversation_id", name="uq_channel_conversation_external"),
        UniqueConstraint("session_id", name="uq_channel_conversation_session"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    channel_account_id: Mapped[str] = mapped_column(String(36), ForeignKey("channel_account.id"), nullable=False)
    external_conversation_id: Mapped[str] = mapped_column(String(300), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)


class NotificationDelivery(Base, TimestampMixin, VersionMixin):
    __tablename__ = "notification_delivery"
    __table_args__ = (
        CheckConstraint("event_type IN ('clarification_required','graph_confirmation_required','tool_confirmation_required','run_completed','run_failed','run_cancelled')", name="ck_notification_delivery_event_type"),
        CheckConstraint("status IN ('queued','delivering','retry_wait','succeeded','failed','cancelled','outcome_unknown')", name="ck_notification_delivery_status"),
        UniqueConstraint("session_id", "dedupe_key", name="uq_notification_delivery_dedupe"),
        UniqueConstraint("channel_account_id", "provider_idempotency_key", name="uq_notification_provider_idempotency"),
        UniqueConstraint("channel_account_id", "external_conversation_id", "provider_message_key", name="uq_notification_provider_message"),
        Index("ix_notification_delivery_claim", "status", "next_attempt_at", "created_at"),
        Index("ix_notification_delivery_session_created", "session_id", "created_at"),
        Index("ix_notification_delivery_run_status", "run_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("run.id"))
    group_run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("group_run.id"))
    triggering_event_id: Mapped[str] = mapped_column(String(36), ForeignKey("execution_event.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    template_key: Mapped[str] = mapped_column(String(100), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    channel_account_id: Mapped[str] = mapped_column(String(36), ForeignKey("channel_account.id"), nullable=False)
    external_conversation_id: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(191), nullable=False)
    provider_idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    provider_message_key: Mapped[str | None] = mapped_column(String(300))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class NotificationDeliveryAttempt(Base, TimestampMixin):
    __tablename__ = "notification_delivery_attempt"
    __table_args__ = (
        CheckConstraint("status IN ('queued','delivering','succeeded','failed','timed_out','outcome_unknown')", name="ck_notification_attempt_status"),
        UniqueConstraint("delivery_id", "attempt_no", name="uq_notification_attempt_no"),
        UniqueConstraint("delivery_id", "provider_request_key", name="uq_notification_attempt_provider_request"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    delivery_id: Mapped[str] = mapped_column(String(36), ForeignKey("notification_delivery.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    claim_token: Mapped[str] = mapped_column(String(36), nullable=False)
    provider_request_key: Mapped[str | None] = mapped_column(String(191))
    provider_message_key: Mapped[str | None] = mapped_column(String(300))
    request_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_metadata: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(default=False, nullable=False)
