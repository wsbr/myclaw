"""Agent-network Group collaboration aggregates."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, uuid7


class GroupSession(Base, TimestampMixin, VersionMixin):
    __tablename__ = "group_session"
    __table_args__ = (
        CheckConstraint("status IN ('open','closed','archived')", name="ck_group_session_status"),
        CheckConstraint(
            "formation_mode IN ('manual_selected','emergent_model_selected')",
            name="ck_group_session_formation_mode",
        ),
        Index("ix_group_session_session_status", "session_id", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    goal: Mapped[str | None] = mapped_column(Text)
    bootstrap_context: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    initiator_agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent.id"), nullable=False
    )
    membership_policy: Mapped[str] = mapped_column(
        String(32), default="fixed_for_run", nullable=False
    )
    formation_mode: Mapped[str] = mapped_column(
        String(32), default="manual_selected", nullable=False
    )

class GroupRun(Base, TimestampMixin, VersionMixin):
    __tablename__ = "group_run"
    __table_args__ = (Index("ix_group_run_session_created", "group_session_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), unique=True, nullable=False)
    group_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_session.id"), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    reporter_agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent.id")
    )
    summary_epoch: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    delivered_summary_epoch: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    shared_context: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    budget_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    policy_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

class GroupRunReference(Base):
    __tablename__ = "group_run_reference"
    __table_args__ = (CheckConstraint("group_run_id != referenced_group_run_id", name="ck_group_run_reference_not_self"),)
    group_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_run.id"), primary_key=True)
    referenced_group_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_run.id"), primary_key=True)

class GroupMembership(Base, TimestampMixin, VersionMixin):
    """Durable membership event/state for one real network Agent."""

    __tablename__ = "group_membership"
    __table_args__ = (
        CheckConstraint(
            "status IN ('invited','joined','declined','cancelled','left','removed')",
            name="ck_group_membership_status",
        ),
        UniqueConstraint(
            "group_session_id", "agent_id", "membership_no",
            name="uq_group_membership_agent_event",
        ),
        Index(
            "ix_group_membership_group_status",
            "group_session_id", "status", "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    group_session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("group_session.id"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent.id"), nullable=False
    )
    membership_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="joined", nullable=False)
    invited_by_kind: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    invited_by_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform_account.id")
    )
    invited_by_agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent.id")
    )
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    context_scope: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    authorization_source: Mapped[str] = mapped_column(
        String(191), default="user_selected", nullable=False
    )
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class GroupRunMember(Base, VersionMixin):
    """Immutable AgentVersion, permission, sharing, and budget snapshot."""

    __tablename__ = "group_run_member"

    group_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("group_run.id"), primary_key=True
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent.id"), primary_key=True
    )
    membership_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("group_membership.id"), nullable=False
    )
    agent_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_version.id")
    )
    participation_status: Mapped[str] = mapped_column(
        String(20), default="available", nullable=False
    )
    unavailable_reason: Mapped[str | None] = mapped_column(String(191))
    membership_policy: Mapped[str] = mapped_column(
        String(32), default="fixed_for_run", nullable=False
    )
    context_scope: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    capability_boundary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    budget_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class GroupSummaryRequest(Base, TimestampMixin, VersionMixin):
    """Unique, auditable Reporter wake-up for one summary epoch."""

    __tablename__ = "group_summary_request"
    __table_args__ = (
        UniqueConstraint(
            "group_run_id", "summary_epoch", name="uq_group_summary_epoch"
        ),
        CheckConstraint(
            "status IN ('queued','running','delivered','superseded','failed')",
            name="ck_group_summary_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    group_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("group_run.id"), nullable=False
    )
    summary_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reporter_work_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("work_item.id"), nullable=False
    )
    trigger_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("execution_event.id")
    )
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    final_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("message.id")
    )


class SharedTask(Base, TimestampMixin, VersionMixin):
    """Optional collaboration whiteboard item; never an execution DAG."""

    __tablename__ = "shared_task"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','in_progress','completed','blocked','cancelled')",
            name="ck_shared_task_status",
        ),
        UniqueConstraint(
            "group_run_id", "source_operation_key",
            name="uq_shared_task_run_operation",
        ),
        Index("ix_shared_task_group_status", "group_session_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    group_session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("group_session.id"), nullable=False
    )
    group_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("group_run.id")
    )
    source_operation_key: Mapped[str | None] = mapped_column(String(191))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    assignee_agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent.id")
    )
    depends_on_task_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    result_refs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
