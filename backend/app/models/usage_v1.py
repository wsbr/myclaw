"""Append-only usage and price facts for quality/economics accounting."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import utcnow, uuid7


class UsagePriceSnapshot(Base):
    """Immutable unit price selected by effective time, never a mutable tariff."""

    __tablename__ = "usage_price_snapshot"
    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('model','tool','sandbox','storage','egress')",
            name="ck_usage_price_subject_type",
        ),
        CheckConstraint(
            "price_status IN ('estimated','contracted')",
            name="ck_usage_price_status",
        ),
        CheckConstraint("price_per_unit >= 0", name="ck_usage_price_nonnegative"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_usage_price_window",
        ),
        UniqueConstraint("content_hash", name="uq_usage_price_content_hash"),
        Index(
            "ix_usage_price_lookup",
            "subject_type", "provider", "subject_key", "metric", "effective_from",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(191))
    subject_key: Mapped[str] = mapped_column(String(191), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    price_per_unit: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    price_status: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    source_label: Mapped[str] = mapped_column(String(500), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )


class UsageLedgerEntry(Base):
    """One immutable measured/estimated quantity and its bound cost snapshot."""

    __tablename__ = "usage_ledger_entry"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('provider_attempt','tool_attempt','sandbox_attempt','storage_observation','egress_observation','adjustment')",
            name="ck_usage_entry_source_type",
        ),
        CheckConstraint(
            "measurement_status IN ('measured','estimated','unknown')",
            name="ck_usage_entry_measurement_status",
        ),
        CheckConstraint(
            "cost_status IN ('estimated','contracted','unknown')",
            name="ck_usage_entry_cost_status",
        ),
        CheckConstraint(
            "source_type = 'adjustment' OR quantity >= 0",
            name="ck_usage_entry_quantity_nonnegative",
        ),
        CheckConstraint(
            "(cost_status = 'unknown' AND price_snapshot_id IS NULL AND currency IS NULL AND unit_price_snapshot IS NULL AND cost_amount IS NULL) OR "
            "(cost_status IN ('estimated','contracted') AND price_snapshot_id IS NOT NULL AND currency IS NOT NULL AND unit_price_snapshot IS NOT NULL AND cost_amount IS NOT NULL)",
            name="ck_usage_entry_cost_binding",
        ),
        UniqueConstraint(
            "source_operation_key", "metric", name="uq_usage_entry_source_metric",
        ),
        Index("ix_usage_entry_run_created", "run_id", "created_at", "id"),
        Index("ix_usage_entry_account_created", "account_id", "created_at", "id"),
        Index("ix_usage_entry_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_usage_entry_agent_created", "agent_version_id", "created_at", "id"),
        Index("ix_usage_entry_model_created", "model_version_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_session.id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    group_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("group_run.id")
    )
    work_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("work_item.id")
    )
    runner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runner_v1.id")
    )
    agent_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_version.id")
    )
    agent_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_turn.id")
    )
    provider_attempt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("provider_attempt.id")
    )
    model_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("model_version.id")
    )
    tool_invocation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_invocation.id")
    )
    tool_attempt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_attempt.id")
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_operation_key: Mapped[str] = mapped_column(String(191), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(30, 9), nullable=False)
    measurement_status: Mapped[str] = mapped_column(String(20), nullable=False)
    price_snapshot_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("usage_price_snapshot.id")
    )
    currency: Mapped[str | None] = mapped_column(String(3))
    unit_price_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    cost_status: Mapped[str] = mapped_column(String(20), nullable=False)
    adjusts_entry_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("usage_ledger_entry.id")
    )
    safe_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )
