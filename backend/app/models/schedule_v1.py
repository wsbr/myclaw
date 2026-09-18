"""Durable user-owned Agent schedules and immutable fire snapshots."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, uuid7


class AgentSchedule(Base, TimestampMixin, VersionMixin):
    """A pure user-owned trigger; execution meaning lives on ToolInvocation."""

    __tablename__ = "agent_schedule"
    __table_args__ = (
        CheckConstraint(
            "schedule_type IN ('once','cron','interval')",
            name="ck_agent_schedule_type",
        ),
        CheckConstraint(
            "status IN ('active','paused','completed','disabled')",
            name="ck_agent_schedule_status",
        ),
        CheckConstraint(
            "version_policy IN ('latest_published','pinned')",
            name="ck_agent_schedule_version_policy",
        ),
        CheckConstraint(
            "overlap_policy IN ('skip','queue_one','allow')",
            name="ck_agent_schedule_overlap_policy",
        ),
        CheckConstraint(
            "misfire_policy IN ('skip','fire_once')",
            name="ck_agent_schedule_misfire_policy",
        ),
        CheckConstraint(
            "context_mode IN ('new_session','continue_session')",
            name="ck_agent_schedule_context_mode",
        ),
        CheckConstraint("misfire_grace_seconds >= 0", name="ck_agent_schedule_misfire_grace"),
        CheckConstraint("total_fire_count >= 0", name="ck_agent_schedule_fire_count"),
        CheckConstraint("occurrence_count >= 0", name="ck_agent_schedule_occurrence_count"),
        CheckConstraint(
            "max_occurrences IS NULL OR max_occurrences >= 1",
            name="ck_agent_schedule_max_occurrences",
        ),
        CheckConstraint(
            "(schedule_type = 'once' AND run_at IS NOT NULL AND cron_expression IS NULL AND interval_seconds IS NULL) OR "
            "(schedule_type = 'cron' AND run_at IS NULL AND cron_expression IS NOT NULL AND interval_seconds IS NULL) OR "
            "(schedule_type = 'interval' AND run_at IS NULL AND cron_expression IS NULL AND interval_seconds IS NOT NULL)",
            name="ck_agent_schedule_shape",
        ),
        CheckConstraint(
            "(version_policy = 'pinned' AND pinned_agent_version_id IS NOT NULL) OR "
            "(version_policy = 'latest_published' AND pinned_agent_version_id IS NULL)",
            name="ck_agent_schedule_version_shape",
        ),
        Index("ix_agent_schedule_due", "status", "next_fire_at", "id"),
        Index("ix_agent_schedule_owner", "tenant_id", "account_id", "status", "updated_at"),
        Index("ix_agent_schedule_agent", "agent_id", "status", "next_fire_at"),
        Index("ix_agent_schedule_source_session", "source_session_id"),
        Index("ix_agent_schedule_lease", "lease_expires_at", "lease_owner"),
        UniqueConstraint(
            "definition_tool_invocation_id",
            name="uq_agent_schedule_definition_tool_invocation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenant.id"), nullable=False)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(191), ForeignKey("user_identity.user_id"), nullable=False
    )
    definition_tool_invocation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_invocation.id")
    )
    # Compatibility-only columns for schedules created before revision 0049.
    # New definitions are read from the referenced immutable ToolInvocation.
    legacy_agent_id: Mapped[str | None] = mapped_column(
        "agent_id", String(36), ForeignKey("agent.id")
    )
    legacy_name: Mapped[str | None] = mapped_column("name", String(200))
    legacy_prompt: Mapped[str | None] = mapped_column("prompt", Text)
    legacy_context_mode: Mapped[str | None] = mapped_column(
        "context_mode", String(24)
    )
    legacy_source_session_id: Mapped[str | None] = mapped_column(
        "source_session_id",
        String(36), ForeignKey("user_session.id")
    )
    schedule_type: Mapped[str] = mapped_column(String(20), nullable=False)
    cron_expression: Mapped[str | None] = mapped_column(String(191))
    interval_seconds: Mapped[int | None] = mapped_column(BigInteger)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    legacy_version_policy: Mapped[str | None] = mapped_column(
        "version_policy", String(32)
    )
    legacy_pinned_agent_version_id: Mapped[str | None] = mapped_column(
        "pinned_agent_version_id", String(36), ForeignKey("agent_version.id")
    )
    overlap_policy: Mapped[str] = mapped_column(String(20), nullable=False)
    misfire_policy: Mapped[str] = mapped_column(String(20), nullable=False)
    misfire_grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    max_occurrences: Mapped[int | None] = mapped_column(BigInteger)
    occurrence_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_fire_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String(191))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class AgentScheduleFire(Base, TimestampMixin):
    """One at-least-once trigger ledger row, linked to one ordinary Run."""

    __tablename__ = "agent_schedule_fire"
    __table_args__ = (
        CheckConstraint(
            "trigger_kind IN ('scheduled','manual')",
            name="ck_agent_schedule_fire_trigger",
        ),
        CheckConstraint(
            "status IN ('queued','deferred','dispatching','running','succeeded','failed','skipped','cancelled')",
            name="ck_agent_schedule_fire_status",
        ),
        CheckConstraint(
            "context_mode_snapshot IN ('new_session','continue_session')",
            name="ck_agent_schedule_fire_context_mode",
        ),
        UniqueConstraint("schedule_id", "operation_key", name="uq_agent_schedule_fire_operation"),
        UniqueConstraint("runner_id", name="uq_agent_schedule_fire_runner"),
        UniqueConstraint("run_id", name="uq_agent_schedule_fire_run"),
        Index("ix_agent_schedule_fire_claim", "status", "scheduled_for", "created_at"),
        Index(
            "ix_agent_schedule_fire_lease",
            "status",
            "lease_expires_at",
            "id",
        ),
        Index("ix_agent_schedule_fire_schedule", "schedule_id", "scheduled_for", "id"),
        Index("ix_agent_schedule_fire_owner", "tenant_id", "account_id", "status", "created_at"),
        Index("ix_agent_schedule_fire_unread", "account_id", "notification_read_at", "completed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    schedule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_schedule.id"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenant.id"), nullable=False)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    trigger_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(191), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(100))
    schedule_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    definition_tool_invocation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_invocation.id")
    )
    definition_hash: Mapped[str | None] = mapped_column(String(64))
    occurrence_no: Mapped[int | None] = mapped_column(BigInteger)
    # Compatibility-only immutable snapshots for pre-0049 fires.
    legacy_prompt_snapshot: Mapped[str | None] = mapped_column("prompt_snapshot", Text)
    legacy_prompt_hash: Mapped[str | None] = mapped_column("prompt_hash", String(64))
    timezone_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    legacy_context_mode_snapshot: Mapped[str | None] = mapped_column(
        "context_mode_snapshot", String(24)
    )
    legacy_source_session_id_snapshot: Mapped[str | None] = mapped_column(
        "source_session_id_snapshot",
        String(36), ForeignKey("user_session.id")
    )
    legacy_version_policy_snapshot: Mapped[str | None] = mapped_column(
        "version_policy_snapshot", String(32)
    )
    agent_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_version.id")
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("user_session.id")
    )
    input_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("message.id")
    )
    run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("run.id"))
    runner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runner_v1.id")
    )
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    notification_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String(191))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class RunnerScheduleSnapshot(Base):
    """Immutable proof of the schedule input and authority used by a Runner."""

    __tablename__ = "runner_schedule_snapshot"
    __table_args__ = (
        UniqueConstraint("fire_id", name="uq_runner_schedule_snapshot_fire"),
        Index("ix_runner_schedule_snapshot_schedule", "schedule_id", "scheduled_for"),
    )

    runner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runner_v1.id"), primary_key=True
    )
    fire_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_schedule_fire.id"), nullable=False
    )
    schedule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_schedule.id"), nullable=False
    )
    schedule_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    definition_tool_invocation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_invocation.id")
    )
    definition_hash: Mapped[str | None] = mapped_column(String(64))
    legacy_prompt_hash: Mapped[str | None] = mapped_column("prompt_hash", String(64))
    agent_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_version.id"), nullable=False
    )
    account_auth_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
