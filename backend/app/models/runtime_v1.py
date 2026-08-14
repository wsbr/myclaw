"""Durable phase-one execution aggregates shared by Chat, Plan, and Group."""

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
from app.models.common import TimestampMixin, VersionMixin, utcnow, uuid7


class Message(Base, TimestampMixin):
    __tablename__ = "message"
    __table_args__ = (
        CheckConstraint("scope_type IN ('chat','group')", name="ck_message_scope"),
        CheckConstraint("sender_type IN ('user','agent','system')", name="ck_message_sender"),
        CheckConstraint("role IN ('user','assistant','system','tool_notice')", name="ck_message_role"),
        CheckConstraint("status IN ('committed','superseded','redacted')", name="ck_message_status"),
        Index("ix_message_session_created", "session_id", "created_at", "id"),
        Index("ix_message_run_created", "run_id", "created_at"),
        UniqueConstraint(
            "group_run_id", "operation_key", name="uq_group_message_operation_key"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(20), default="chat", nullable=False)
    group_session_id: Mapped[str | None] = mapped_column(String(36))
    group_run_id: Mapped[str | None] = mapped_column(String(36))
    sender_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sender_user_id: Mapped[str | None] = mapped_column(String(191))
    sender_agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent.id"))
    recipient_type: Mapped[str] = mapped_column(String(20), default="session", nullable=False)
    recipient_membership_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("group_membership.id")
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_format: Mapped[str] = mapped_column(String(20), default="markdown", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="committed", nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36))
    work_item_id: Mapped[str | None] = mapped_column(String(36))
    clarification_id: Mapped[str | None] = mapped_column(String(36))
    reply_to_message_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("message.id"))
    caused_by_event_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("execution_event.id"))
    # Agent-native Group routing also records the exact membership principal.
    visibility: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    recipient_agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent.id")
    )
    sequence_no: Mapped[int | None] = mapped_column(BigInteger)
    operation_key: Mapped[str | None] = mapped_column(String(191))


class Run(Base, TimestampMixin, VersionMixin):
    __tablename__ = "run"
    __table_args__ = (
        CheckConstraint("mode IN ('chat','plan','group')", name="ck_run_mode"),
        CheckConstraint(
            "status IN ('active','waiting_user','waiting_approval','waiting_tool','waiting_agent','completed','cancelled','failed')",
            name="ck_run_status",
        ),
        CheckConstraint(
            "(status IN ('waiting_user','waiting_approval','waiting_tool','waiting_agent') AND wait_reason = status) OR "
            "(status NOT IN ('waiting_user','waiting_approval','waiting_tool','waiting_agent') AND wait_reason IS NULL)",
            name="ck_run_wait_reason",
        ),
        CheckConstraint(
            "(status IN ('completed','cancelled','failed') AND completed_at IS NOT NULL) OR "
            "(status NOT IN ('completed','cancelled','failed') AND completed_at IS NULL)",
            name="ck_run_terminal_time",
        ),
        UniqueConstraint("id", "user_id", name="uq_run_id_user"),
        UniqueConstraint("id", "tenant_id", name="uq_run_id_tenant"),
        Index("ix_run_session_created", "session_id", "created_at"),
        Index("ix_run_user_id", "user_id"),
        Index("ix_run_status_updated", "status", "updated_at", "id"),
        Index("ix_run_tenant_account_created", "tenant_id", "account_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    wait_reason: Mapped[str | None] = mapped_column(String(32))
    root_work_item_id: Mapped[str | None] = mapped_column(String(36))
    permission_mode: Mapped[str] = mapped_column(
        String(32), default="direct", nullable=False
    )
    hard_budget_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    terminal_reason: Mapped[str | None] = mapped_column(String(100))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class WorkerInstance(Base, TimestampMixin):
    """Process-level liveness and bounded slot capacity for Runtime operators."""

    __tablename__ = "worker_instance"
    __table_args__ = (
        CheckConstraint("capacity > 0", name="ck_worker_instance_capacity_positive"),
        CheckConstraint(
            "active_slots >= 0 AND active_slots <= capacity",
            name="ck_worker_instance_active_slots",
        ),
        CheckConstraint(
            "status IN ('active','stopped')", name="ck_worker_instance_status",
        ),
        Index("ix_worker_instance_kind_heartbeat", "worker_kind", "heartbeat_at"),
    )

    worker_id: Mapped[str] = mapped_column(String(191), primary_key=True)
    worker_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    active_slots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    process_id: Mapped[int] = mapped_column(Integer, nullable=False)
    release_id: Mapped[str] = mapped_column(String(191), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False,
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False,
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class WorkItem(Base, TimestampMixin, VersionMixin):
    __tablename__ = "work_item"
    __table_args__ = (
        CheckConstraint("mode IN ('chat','plan','group')", name="ck_work_item_mode"),
        CheckConstraint(
            "status IN ('queued','running','waiting_user','waiting_approval','waiting_agent','waiting_tool','waiting_resource','succeeded','failed','timed_out','cancelled','abandoned','blocked')",
            name="ck_work_item_status",
        ),
        UniqueConstraint("id", "user_id", name="uq_work_item_id_user"),
        UniqueConstraint("id", "tenant_id", name="uq_work_item_id_tenant"),
        Index("ix_work_item_run_status", "run_id", "status"),
        Index("ix_work_item_status_updated", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    owner_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    agent_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_version.id"), nullable=False)
    parent_work_item_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("work_item.id"))
    group_run_id: Mapped[str | None] = mapped_column(String(36))
    group_membership_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("group_membership.id")
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(64), default="root_request", nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    wait_reason: Mapped[str | None] = mapped_column(String(64))
    resume_cursor: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    active_runner_id: Mapped[str | None] = mapped_column(String(36))
    execution_epoch: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text)
    result_data: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class DurableRunner(Base, TimestampMixin, VersionMixin):
    __tablename__ = "runner_v1"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled','lost')", name="ck_runner_v1_status"),
        UniqueConstraint("work_item_id", "execution_epoch", name="uq_runner_work_epoch"),
        UniqueConstraint("id", "tenant_id", name="uq_runner_id_tenant"),
        Index("ix_runner_v1_lease", "status", "lease_expires_at"),
        Index("ix_runner_v1_session_created", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False
    )
    work_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("work_item.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    agent_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_version.id"), nullable=False)
    execution_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(191))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    capability_snapshot_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey(
            "capability_snapshot.id", use_alter=True,
            name="fk_runner_capability_snapshot",
        )
    )
    principal_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    assembly_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    prompt_manifest: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result_message_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("message.id"))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    failure_code: Mapped[str | None] = mapped_column(String(100))


class RunnerCheckpoint(Base):
    __tablename__ = "runner_checkpoint"
    __table_args__ = (
        UniqueConstraint("runner_id", "sequence_no", name="uq_runner_checkpoint_sequence"),
        Index("ix_runner_checkpoint_work_safe", "work_item_id", "safe_to_resume", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_v1.id"), nullable=False)
    work_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("work_item.id"), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    checkpoint_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    execution_epoch: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    capability_snapshot_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("capability_snapshot.id")
    )
    context_manifest: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    compaction_ref: Mapped[str | None] = mapped_column(String(500))
    working_plan_revision: Mapped[int | None] = mapped_column(BigInteger)
    result_cursor: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    pending_refs: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    loaded_tool_version_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    presented_schema_bundle_hash: Mapped[str | None] = mapped_column(String(64))
    budget_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    last_completed_step: Mapped[str | None] = mapped_column(String(100))
    safe_to_resume: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class CapabilitySnapshot(Base):
    """Immutable effective capability set resolved for one Runner."""

    __tablename__ = "capability_snapshot"
    __table_args__ = (
        UniqueConstraint("runner_id", name="uq_capability_snapshot_runner"),
        Index("ix_capability_snapshot_work_item", "work_item_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    runner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runner_v1.id"), nullable=False
    )
    work_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("work_item.id"), nullable=False
    )
    agent_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_version.id"), nullable=False
    )
    execution_principal: Mapped[dict] = mapped_column(JSON, nullable=False)
    assembled_tool_keys: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    effective_tool_keys: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    authorized_tool_versions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    denied_capabilities: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    discoverable_agents_scope: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    callable_agents_scope: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    permission_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    governance_versions: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    budget_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    runtime_release_id: Mapped[str] = mapped_column(String(64), nullable=False)
    harness_policy_versions: Mapped[dict] = mapped_column(JSON, nullable=False)
    provider_adapter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_schema_bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_cohort: Mapped[str | None] = mapped_column(String(100))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )


class AgentTurn(Base):
    """One logical model decision, independent from transport retries."""

    __tablename__ = "agent_turn"
    __table_args__ = (
        CheckConstraint(
            "status IN ('started','waiting_actions','completed','failed')",
            name="ck_agent_turn_status",
        ),
        UniqueConstraint("runner_id", "turn_no", name="uq_agent_turn_number"),
        Index("ix_agent_turn_work_item", "work_item_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    runner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runner_v1.id"), nullable=False
    )
    work_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("work_item.id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    agent_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_version.id"), nullable=False
    )
    capability_snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("capability_snapshot.id"), nullable=False
    )
    turn_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="started", nullable=False)
    context_manifest: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    turn_items: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    presented_schema_bundle_hash: Mapped[str | None] = mapped_column(String(64))
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    schema_repair_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usage: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    provider_state_ref: Mapped[str | None] = mapped_column(String(500))
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )


class ProviderAttempt(Base):
    """Append-oriented transport attempt for an AgentTurn."""

    __tablename__ = "provider_attempt"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','timed_out','outcome_unknown')",
            name="ck_provider_attempt_status",
        ),
        UniqueConstraint("agent_turn_id", "attempt_no", name="uq_provider_attempt_number"),
        Index("ix_provider_attempt_status", "status", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    agent_turn_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_turn.id"), nullable=False
    )
    model_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("model_version.id"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(String(191), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(191))
    provider_state_ref: Mapped[str | None] = mapped_column(String(500))
    received_payload: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tool_call_observed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    side_effect_committed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    usage: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    first_payload_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )


class WorkingPlan(Base, TimestampMixin, VersionMixin):
    """Current model-maintained plan for one Agent in one Run.

    A WorkingPlan is context/UI state only.  It deliberately has no foreign
    key to a plan node or any scheduling field that could create WorkItems.
    """

    __tablename__ = "working_plan"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','superseded','closed')",
            name="ck_working_plan_status",
        ),
        Index(
            "ix_working_plan_run_owner_status",
            "run_id", "owner_agent_id", "status", "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_session.id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("run.id"), nullable=False
    )
    owner_agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent.id"), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)


class PlanUpdate(Base):
    """Append-only revision payload for a WorkingPlan."""

    __tablename__ = "plan_update"
    __table_args__ = (
        UniqueConstraint(
            "working_plan_id", "revision", name="uq_plan_update_revision"
        ),
        Index("ix_plan_update_plan_created", "working_plan_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    working_plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("working_plan.id"), nullable=False
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    items: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text)
    updated_by_runner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runner_v1.id"), nullable=False
    )
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_turn.id")
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )


class Clarification(Base, TimestampMixin, VersionMixin):
    __tablename__ = "clarification"
    __table_args__ = (
        CheckConstraint("status IN ('open','answered','cancelled')", name="ck_clarification_status"),
        UniqueConstraint("work_item_id", "idempotency_key", name="uq_clarification_work_key"),
        Index("ix_clarification_target_status_created", "target_user_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    work_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("work_item.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    group_session_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("group_session.id"))
    group_membership_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("group_membership.id")
    )
    asked_by_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    target_user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    answer_message_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("message.id"))
    answer_text: Mapped[str | None] = mapped_column(Text)
    answered_by_user_id: Mapped[str | None] = mapped_column(String(191), ForeignKey("user_identity.user_id"))
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_turn.id")
    )
    provider_tool_call_id: Mapped[str | None] = mapped_column(String(191))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class Approval(Base, TimestampMixin, VersionMixin):
    """Unified human approval for exact versioned subjects."""

    __tablename__ = "approval"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('plan_implementation','tool','membership','session_grant','reporter_selection')",
            name="ck_approval_kind",
        ),
        CheckConstraint(
            "status IN ('pending','approved','rejected','cancelled')",
            name="ck_approval_status",
        ),
        UniqueConstraint(
            "kind", "subject_type", "subject_id", "subject_hash",
            name="uq_approval_subject",
        ),
        UniqueConstraint(
            "work_item_id", "idempotency_key", name="uq_approval_work_key"
        ),
        Index(
            "ix_approval_target_status_created",
            "target_account_id", "status", "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_session.id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    work_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("work_item.id"), nullable=False
    )
    requested_by_agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subject_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Immutable inputs used to recompute the subject hash at decision time.
    # This is deliberately authorization metadata, not presentation text.
    subject_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    presentation: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_by_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform_account.id")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class SessionGrant(Base, TimestampMixin, VersionMixin):
    """Revocable, time-bounded authorization scoped to one Session Tool."""

    __tablename__ = "session_grant"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','revoked','expired')", name="ck_session_grant_status"
        ),
        Index(
            "ix_session_grant_match",
            "session_id", "target_account_id", "tool_key", "status", "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_session.id"), nullable=False
    )
    target_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent.id"), nullable=False
    )
    tool_key: Mapped[str] = mapped_column(String(191), nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    source_approval_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("approval.id"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class SessionSandbox(Base, TimestampMixin, VersionMixin):
    __tablename__ = "session_sandbox"
    __table_args__ = (
        CheckConstraint("status IN ('provisioning','ready','failed','cleaned')", name="ck_session_sandbox_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), unique=True, nullable=False)
    sandbox_profile_id: Mapped[str] = mapped_column(String(36), ForeignKey("sandbox_profile.id"), nullable=False)
    profile_config_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="provisioning", nullable=False)
    root_uri: Mapped[str] = mapped_column(String(1000), nullable=False)


class Workspace(Base, TimestampMixin, VersionMixin):
    __tablename__ = "workspace"
    __table_args__ = (
        CheckConstraint("status IN ('active','cleaning','cleaned','failed')", name="ck_workspace_status"),
        UniqueConstraint("id", "tenant_id", name="uq_workspace_id_tenant"),
        Index("ix_workspace_tenant_session", "tenant_id", "session_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False
    )
    session_sandbox_id: Mapped[str] = mapped_column(String(36), ForeignKey("session_sandbox.id"), unique=True, nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), unique=True, nullable=False)
    root_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    retention_policy_key: Mapped[str] = mapped_column(String(64), default="session_lifetime", nullable=False)


class WorkspaceLock(Base, TimestampMixin, VersionMixin):
    __tablename__ = "workspace_lock"

    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspace.id"), primary_key=True)
    writer_request_id: Mapped[str | None] = mapped_column(String(36))
    writer_fencing_token: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    writer_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    next_ticket: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)


class WorkspaceLockRequest(Base, TimestampMixin, VersionMixin):
    __tablename__ = "workspace_lock_request"
    __table_args__ = (
        CheckConstraint("mode IN ('read','write')", name="ck_workspace_lock_request_mode"),
        CheckConstraint(
            "status IN ('queued','granted','released','expired')",
            name="ck_workspace_lock_request_status",
        ),
        Index("ix_workspace_lock_request_queue", "workspace_id", "status", "ticket"),
        Index("ix_workspace_lock_request_tool", "tool_invocation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspace.id"), nullable=False)
    tool_invocation_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tool_invocation.id"))
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    ticket: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    fencing_token: Mapped[int | None] = mapped_column(BigInteger)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class WorkspaceReadLease(Base, TimestampMixin):
    __tablename__ = "workspace_read_lease"
    __table_args__ = (Index("ix_workspace_read_lease_expiry", "workspace_id", "expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspace.id"), nullable=False)
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspace_lock_request.id"), unique=True, nullable=False
    )
    ticket: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_token: Mapped[str] = mapped_column(String(36), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class ToolInvocation(Base, TimestampMixin, VersionMixin):
    __tablename__ = "tool_invocation"
    __table_args__ = (
        UniqueConstraint("work_item_id", "business_idempotency_key", name="uq_tool_invocation_business_key"),
        UniqueConstraint(
            "work_item_id", "source_turn_id", "provider_tool_call_id",
            name="uq_tool_invocation_provider_call",
        ),
        CheckConstraint(
            "status IN ('created','waiting_confirmation','waiting_resource','queued','running','succeeded','failed','rejected','cancel_requested','cancelled','outcome_unknown')",
            name="ck_tool_invocation_status",
        ),
        Index("ix_tool_invocation_status_updated", "status", "updated_at"),
        Index("ix_tool_invocation_tool_status", "platform_tool_id", "status"),
        Index("ix_tool_invocation_tenant_account", "tenant_id", "account_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_v1.id"), nullable=False)
    work_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("work_item.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    platform_tool_id: Mapped[str] = mapped_column(String(36), ForeignKey("platform_tool.id"), nullable=False)
    tool_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tool_version.id"), nullable=False
    )
    tool_version_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_connection.id")
    )
    connection_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_connection_revision.id")
    )
    secret_fingerprint: Mapped[str | None] = mapped_column(String(64))
    tool_key: Mapped[str] = mapped_column(String(191), nullable=False)
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_turn.id")
    )
    provider_tool_call_id: Mapped[str | None] = mapped_column(String(191))
    operation_key: Mapped[str | None] = mapped_column(String(191))
    capability_snapshot_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("capability_snapshot.id")
    )
    authorization_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    business_idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    principal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_data: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_attempt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey(
            "tool_attempt.id", use_alter=True,
            name="fk_tool_invocation_active_attempt",
        )
    )
    execution_epoch: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class ToolAttempt(Base, TimestampMixin):
    __tablename__ = "tool_attempt"
    __table_args__ = (
        UniqueConstraint("tool_invocation_id", "attempt_no", name="uq_tool_attempt_number"),
        UniqueConstraint("tool_invocation_id", "provider_request_id", name="uq_tool_attempt_provider_request"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','timed_out','cancelled','outcome_unknown')",
            name="ck_tool_attempt_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tool_invocation_id: Mapped[str] = mapped_column(String(36), ForeignKey("tool_invocation.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(191))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    provider_request_id: Mapped[str | None] = mapped_column(String(191))
    request_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_snapshot: Mapped[dict | None] = mapped_column(JSON)
    error_class: Mapped[str | None] = mapped_column(String(191))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    execution_epoch: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    side_effect_started: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    outcome_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class ToolConfirmation(Base, TimestampMixin, VersionMixin):
    __tablename__ = "tool_confirmation"
    __table_args__ = (
        CheckConstraint("status IN ('pending','approved','rejected','cancelled')", name="ck_tool_confirmation_status"),
        Index("ix_tool_confirmation_user_status", "user_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tool_invocation_id: Mapped[str] = mapped_column(String(36), ForeignKey("tool_invocation.id"), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    tool_key: Mapped[str] = mapped_column(String(191), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    principal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    decision_by_user_id: Mapped[str | None] = mapped_column(String(191))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class AgentInvocation(Base, TimestampMixin, VersionMixin):
    __tablename__ = "agent_invocation"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created','queued','running','succeeded','failed','cancelled','timed_out','rejected')",
            name="ck_agent_invocation_status",
        ),
        CheckConstraint(
            "return_route IN ('caller','direct','group')",
            name="ck_agent_invocation_return_route",
        ),
        UniqueConstraint("caller_work_item_id", "idempotency_key", name="uq_agent_invocation_business_key"),
        UniqueConstraint("caller_work_item_id", "operation_key", name="uq_agent_invocation_operation"),
        UniqueConstraint("target_work_item_id", name="uq_agent_invocation_target_work"),
        Index("ix_agent_invocation_caller_batch_status", "caller_work_item_id", "batch_key", "status"),
        Index("ix_agent_invocation_run_status", "run_id", "status", "created_at"),
        Index("ix_agent_invocation_target_work", "target_work_item_id"),
        Index("ix_agent_invocation_tenant_account", "tenant_id", "account_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    caller_work_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("work_item.id"), nullable=False)
    caller_runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_v1.id"), nullable=False)
    caller_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    target_work_item_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("work_item.id"))
    target_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    target_agent_version_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent_version.id"))
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    return_route: Mapped[str] = mapped_column(String(20), default="caller", nullable=False)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    context_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="created", nullable=False)
    batch_key: Mapped[str | None] = mapped_column(String(100))
    result_summary: Mapped[str | None] = mapped_column(Text)
    result_data: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(191), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result_message_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("message.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    group_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("group_session.id")
    )
    group_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("group_run.id")
    )
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class RunnerSkillSnapshot(Base):
    __tablename__ = "runner_skill_snapshot"
    runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_v1.id"), primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(36), ForeignKey("skill.id"), primary_key=True)
    skill_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("skill_version.id"), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    path_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class RunnerToolSnapshot(Base):
    __tablename__ = "runner_tool_snapshot"
    runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_v1.id"), primary_key=True)
    platform_tool_id: Mapped[str] = mapped_column(String(36), ForeignKey("platform_tool.id"), primary_key=True)
    tool_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tool_version.id"), nullable=False
    )
    tool_version_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    connection_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_connection_revision.id")
    )
    secret_fingerprint: Mapped[str | None] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class BrowserSessionState(Base, TimestampMixin, VersionMixin):
    """Durable control-plane state for an isolated Chromium Session."""

    __tablename__ = "browser_session_state"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new','ready','closed','failed','outcome_unknown')",
            name="ck_browser_session_status",
        ),
        Index("ix_browser_session_status_updated", "status", "updated_at"),
    )

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_session.id"), primary_key=True
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspace.id")
    )
    worker_instance_id: Mapped[str | None] = mapped_column(String(191))
    status: Mapped[str] = mapped_column(String(32), default="new", nullable=False)
    page_revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    current_url: Mapped[str | None] = mapped_column(String(2000))
    current_origin: Mapped[str | None] = mapped_column(String(500))
    dom_fingerprint: Mapped[str | None] = mapped_column(String(64))
    last_operation: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class RunnerPolicySnapshot(Base):
    __tablename__ = "runner_policy_snapshot"
    __table_args__ = (UniqueConstraint("runner_id", "policy_type", name="uq_runner_policy_type"),)
    runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_v1.id"), primary_key=True)
    policy_definition_id: Mapped[str] = mapped_column(String(36), ForeignKey("policy_definition.id"), primary_key=True)
    parameter_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_type: Mapped[str] = mapped_column(String(32), nullable=False)
    parameters_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
