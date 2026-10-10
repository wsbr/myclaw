"""Versioned Agent evaluation suites, executions, and release evidence."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, Numeric,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, utcnow, uuid7


class AgentEvalSuite(Base, TimestampMixin, VersionMixin):
    """Stable release-gate identity whose current version may advance."""

    __tablename__ = "agent_eval_suite"
    __table_args__ = (
        CheckConstraint("status IN ('active','inactive')", name="ck_agent_eval_suite_status"),
        Index("ix_agent_eval_suite_agent_status", "agent_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_eval_suite_version.id", use_alter=True)
    )
    next_version_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )


class AgentEvalSuiteVersion(Base):
    """Immutable case bundle and threshold used by release decisions."""

    __tablename__ = "agent_eval_suite_version"
    __table_args__ = (
        UniqueConstraint("suite_id", "version_no", name="uq_agent_eval_suite_version_no"),
        CheckConstraint("version_no > 0", name="ck_agent_eval_suite_version_positive"),
        Index("ix_agent_eval_suite_version_suite", "suite_id", "version_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    suite_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_eval_suite.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    gate_policy: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )


class AgentEvalCase(Base):
    """One immutable input and deterministic assertion contract."""

    __tablename__ = "agent_eval_case"
    __table_args__ = (
        UniqueConstraint("suite_version_id", "case_key", name="uq_agent_eval_case_key"),
        CheckConstraint("weight > 0", name="ck_agent_eval_case_weight_positive"),
        Index("ix_agent_eval_case_suite_order", "suite_version_id", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    suite_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_eval_suite_version.id"), nullable=False
    )
    case_key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    assertions: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    case_metadata: Mapped[dict | None] = mapped_column(JSON)
    weight: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("1"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )


class AgentEvalRun(Base, TimestampMixin):
    """Execution against one exact candidate assembly and suite version."""

    __tablename__ = "agent_eval_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','passed','failed','error')",
            name="ck_agent_eval_run_status",
        ),
        CheckConstraint(
            "(status IN ('passed','failed','error') AND ended_at IS NOT NULL) OR "
            "(status IN ('queued','running') AND ended_at IS NULL)",
            name="ck_agent_eval_run_terminal_time",
        ),
        Index(
            "ix_agent_eval_run_gate_lookup", "agent_id", "suite_version_id",
            "candidate_content_hash", "status", "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    suite_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_eval_suite_version.id"), nullable=False
    )
    candidate_agent_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_version.id"), nullable=False
    )
    candidate_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    passed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(12, 9))
    gate_policy_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_by_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )


class AgentEvalCaseRun(Base, TimestampMixin):
    """Mutable settlement state for one case; bindings never change."""

    __tablename__ = "agent_eval_case_run"
    __table_args__ = (
        UniqueConstraint("eval_run_id", "eval_case_id", name="uq_agent_eval_case_run"),
        UniqueConstraint("run_id", name="uq_agent_eval_case_runtime_run"),
        CheckConstraint(
            "status IN ('queued','running','passed','failed','error')",
            name="ck_agent_eval_case_run_status",
        ),
        Index("ix_agent_eval_case_run_status", "status", "updated_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    eval_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_eval_run.id"), nullable=False
    )
    eval_case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_eval_case.id"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_session.id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(12, 9))
    response_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("message.id")
    )
    assertion_results: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
