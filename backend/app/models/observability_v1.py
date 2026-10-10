"""Non-authoritative Runner-scoped observability traces and span links."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import CreatedAtMixin, utcnow, uuid7


class RunnerTrace(Base):
    __tablename__ = "runner_trace"
    __table_args__ = (
        CheckConstraint("status IN ('running','completed','failed','cancelled')", name="ck_runner_trace_status"),
        Index("ix_runner_trace_run_created", "run_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    trace_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    root_span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_v1.id"), unique=True, nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    work_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("work_item.id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    sampled: Mapped[bool] = mapped_column(default=True, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class ObservabilitySpan(Base):
    __tablename__ = "observability_span"
    __table_args__ = (
        CheckConstraint("status IN ('ok','error')", name="ck_observability_span_status"),
        Index("ix_observability_span_trace_started", "runner_trace_id", "started_at"),
        UniqueConstraint("projection_key", name="uq_observability_span_projection"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    runner_trace_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_trace.id"), nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    span_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    # Only ledger projections have a key. Ordinary spans may repeat, and legacy
    # duplicate spans remain intact (including their links) with a NULL key.
    projection_key: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)


class ObservabilitySpanLink(Base, CreatedAtMixin):
    __tablename__ = "observability_span_link"
    __table_args__ = (UniqueConstraint("span_id", "linked_trace_id", "linked_span_id", "link_type", name="uq_observability_span_link"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    span_id: Mapped[str] = mapped_column(String(36), ForeignKey("observability_span.id"), nullable=False)
    linked_trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    linked_span_id: Mapped[str | None] = mapped_column(String(16))
    link_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(36))
