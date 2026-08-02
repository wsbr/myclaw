"""Visibility-scoped, replaceable caches for compressed Session history."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger, CheckConstraint, Computed, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, case, column, literal,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, uuid7


class HistorySummary(Base, TimestampMixin, VersionMixin):
    __tablename__ = "history_summary"
    __table_args__ = (
        CheckConstraint("scope_type IN ('chat','group_agent')", name="ck_history_summary_scope"),
        CheckConstraint("status IN ('pending','ready','failed','superseded')", name="ck_history_summary_status"),
        CheckConstraint(
            "(scope_type = 'chat' AND group_session_id IS NULL AND group_node_id IS NULL) OR "
            "(scope_type = 'group_agent' AND group_session_id IS NOT NULL AND group_node_id IS NOT NULL)",
            name="ck_history_summary_scope_fields",
        ),
        UniqueConstraint(
            "session_id", "scope_discriminator", "through_event_sequence",
            name="uq_history_summary_scope_sequence",
        ),
        Index("ix_history_summary_ready", "session_id", "scope_discriminator", "status", "through_event_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    group_session_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("group_session.id"))
    group_node_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("group_agent_node.id"))
    scope_discriminator: Mapped[str] = mapped_column(
        String(191),
        Computed(
            case(
                (column("scope_type") == literal("chat"), literal("chat")),
                else_=literal("group_node:") + column("group_node_id"),
            ),
            persisted=True,
        ),
    )
    through_message_id: Mapped[str] = mapped_column(String(36), ForeignKey("message.id"), nullable=False)
    through_event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    model_id: Mapped[str] = mapped_column(String(36), ForeignKey("model.id"), nullable=False)
    parameter_version: Mapped[int] = mapped_column(Integer, nullable=False)
