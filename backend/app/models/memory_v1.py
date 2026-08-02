"""Versioned user Memory heads, immutable facts, candidates, and Runner recall snapshots."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, CheckConstraint, Computed, DateTime, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, uuid7


class MemoryHead(Base, TimestampMixin, VersionMixin):
    __tablename__ = "memory_head"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'user' AND agent_id IS NULL) OR (scope = 'user_agent' AND agent_id IS NOT NULL)",
            name="ck_memory_head_scope_agent",
        ),
        UniqueConstraint("user_id", "scope_discriminator", "fact_key", name="uq_memory_head_logical_key"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent.id"))
    fact_key: Mapped[str] = mapped_column(String(255), nullable=False)
    scope_discriminator: Mapped[str] = mapped_column(
        String(64), Computed("CASE WHEN scope = 'user' THEN 'user' ELSE 'user_agent:' || agent_id END", persisted=True)
    )
    last_user_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class MemoryRecord(Base, TimestampMixin, VersionMixin):
    __tablename__ = "memory"
    __table_args__ = (
        CheckConstraint("scope IN ('user','user_agent','agent')", name="ck_memory_scope"),
        CheckConstraint("status IN ('active','superseded','deleted')", name="ck_memory_status"),
        CheckConstraint("source_kind IN ('model','user_edit','import')", name="ck_memory_source_kind"),
        UniqueConstraint("active_head_id", name="uq_memory_one_active"),
        Index("ix_memory_user_scope_status_updated", "user_id", "scope", "agent_id", "status", "updated_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    head_id: Mapped[str] = mapped_column(String(36), ForeignKey("memory_head.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent.id"))
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    fact_key: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    active_head_id: Mapped[str | None] = mapped_column(
        String(36), Computed("CASE WHEN status = 'active' THEN head_id ELSE NULL END", persisted=True)
    )
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_session_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("user_session.id"))
    created_by_runner_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("runner_v1.id"))
    policy_definition_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("policy_definition.id"))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    supersedes_memory_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("memory.id"))


class MemorySourceMessage(Base):
    __tablename__ = "memory_source_message"
    memory_id: Mapped[str] = mapped_column(String(36), ForeignKey("memory.id"), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(36), ForeignKey("message.id"), primary_key=True)
    source_role: Mapped[str] = mapped_column(String(20), nullable=False)


class RunnerMemorySnapshot(Base):
    __tablename__ = "runner_memory_snapshot"
    runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_v1.id"), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(36), ForeignKey("memory.id"), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)


class MemoryWriteCandidate(Base, TimestampMixin, VersionMixin):
    __tablename__ = "memory_write_candidate"
    __table_args__ = (
        CheckConstraint("scope IN ('user','user_agent')", name="ck_memory_candidate_scope"),
        CheckConstraint("status IN ('pending','accepted','rejected','failed')", name="ck_memory_candidate_status"),
        Index("ix_memory_candidate_status_created", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent.id"))
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    fact_key: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_v1.id"), nullable=False)
    policy_definition_id: Mapped[str] = mapped_column(String(36), ForeignKey("policy_definition.id"), nullable=False)
    observed_head_version: Mapped[int | None] = mapped_column(BigInteger)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    rejection_code: Mapped[str | None] = mapped_column(String(100))
    result_memory_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("memory.id"))


class MemoryCandidateSourceMessage(Base):
    __tablename__ = "memory_candidate_source_message"
    candidate_id: Mapped[str] = mapped_column(String(36), ForeignKey("memory_write_candidate.id"), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(36), ForeignKey("message.id"), primary_key=True)
    source_role: Mapped[str] = mapped_column(String(20), nullable=False)
