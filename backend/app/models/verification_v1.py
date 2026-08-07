"""Deterministic verification and cross-ledger reliability facts."""

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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import utcnow, uuid7


class VerificationResult(Base):
    """Immutable result produced by one versioned deterministic rule."""

    __tablename__ = "verification_result"
    __table_args__ = (
        CheckConstraint(
            "status IN ('passed','failed','insufficient_evidence')",
            name="ck_verification_result_status",
        ),
        UniqueConstraint(
            "run_id", "rule_key", "rule_version", "verification_epoch",
            name="uq_verification_result_epoch",
        ),
        UniqueConstraint(
            "run_id", "rule_key", "rule_version", "fact_fingerprint",
            name="uq_verification_result_facts",
        ),
        Index(
            "ix_verification_result_run_rule",
            "run_id", "rule_key", "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("run.id"), nullable=False
    )
    group_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("group_run.id")
    )
    rule_key: Mapped[str] = mapped_column(String(191), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    fact_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_facts: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    missing_facts: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )


class VerificationEvidenceRef(Base):
    """Typed reference from a result to an authoritative execution fact."""

    __tablename__ = "verification_evidence_ref"
    __table_args__ = (
        CheckConstraint(
            "evidence_type IN ('tool_attempt','artifact','workspace_checksum','business_fact')",
            name="ck_verification_evidence_type",
        ),
        CheckConstraint(
            "(evidence_type = 'tool_attempt' AND tool_attempt_id IS NOT NULL "
            "AND artifact_id IS NULL AND external_fact_ref IS NULL "
            "AND workspace_generation IS NULL) OR "
            "(evidence_type = 'artifact' AND artifact_id IS NOT NULL "
            "AND tool_attempt_id IS NULL AND external_fact_ref IS NULL "
            "AND workspace_generation IS NULL AND checksum IS NOT NULL) OR "
            "(evidence_type = 'workspace_checksum' AND tool_attempt_id IS NULL "
            "AND artifact_id IS NULL AND external_fact_ref IS NOT NULL "
            "AND workspace_generation IS NOT NULL AND checksum IS NOT NULL) OR "
            "(evidence_type = 'business_fact' AND tool_attempt_id IS NULL "
            "AND artifact_id IS NULL AND external_fact_ref IS NOT NULL "
            "AND workspace_generation IS NULL AND checksum IS NOT NULL)",
            name="ck_verification_evidence_ref_shape",
        ),
        UniqueConstraint(
            "verification_result_id", "evidence_type", "evidence_key",
            name="uq_verification_evidence_key",
        ),
    )

    verification_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("verification_result.id"), primary_key=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, primary_key=True)
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_attempt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_attempt.id")
    )
    artifact_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("artifact.id")
    )
    external_fact_ref: Mapped[str | None] = mapped_column(String(2048))
    workspace_generation: Mapped[int | None] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(String(64))
    evidence_key: Mapped[str] = mapped_column(String(191), nullable=False)


class RunProgressMark(Base):
    """Append-only no-progress diagnostic; it never settles a Run."""

    __tablename__ = "run_progress_mark"
    __table_args__ = (
        UniqueConstraint("agent_turn_id", name="uq_run_progress_agent_turn"),
        Index("ix_run_progress_run_created", "run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("run.id"), nullable=False
    )
    work_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("work_item.id"), nullable=False
    )
    runner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runner_v1.id"), nullable=False
    )
    agent_turn_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_turn.id"), nullable=False
    )
    made_progress: Mapped[bool] = mapped_column(Boolean, nullable=False)
    progress_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    consecutive_no_progress: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )


class LateResultObservation(Base):
    """Immutable evidence for a result arriving after its owner became terminal."""

    __tablename__ = "late_result_observation"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('tool_attempt','provider_attempt','agent_invocation')",
            name="ck_late_result_source_type",
        ),
        UniqueConstraint(
            "source_type", "source_id", "result_hash",
            name="uq_late_result_source_hash",
        ),
        Index("ix_late_result_run_observed", "run_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("run.id"), nullable=False
    )
    work_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("work_item.id")
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    original_status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    safe_result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )
