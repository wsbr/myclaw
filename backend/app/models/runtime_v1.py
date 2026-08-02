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
    recipient_group_node_id: Mapped[str | None] = mapped_column(String(36))
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_format: Mapped[str] = mapped_column(String(20), default="markdown", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="committed", nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36))
    work_item_id: Mapped[str | None] = mapped_column(String(36))
    clarification_id: Mapped[str | None] = mapped_column(String(36))
    reply_to_message_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("message.id"))
    caused_by_event_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("execution_event.id"))


class Run(Base, TimestampMixin, VersionMixin):
    __tablename__ = "run"
    __table_args__ = (
        CheckConstraint("mode IN ('chat','plan','group')", name="ck_run_mode"),
        CheckConstraint("execution_kind IN ('normal','draft_test','proposal_generation')", name="ck_run_kind"),
        CheckConstraint("lifecycle IN ('running','waiting','settling','completed','cancelled')", name="ck_run_lifecycle"),
        CheckConstraint("outcome IS NULL OR outcome IN ('succeeded','partially_succeeded','failed')", name="ck_run_outcome"),
        CheckConstraint("(lifecycle = 'waiting' AND wait_reason IS NOT NULL) OR (lifecycle != 'waiting' AND wait_reason IS NULL)", name="ck_run_wait_reason"),
        CheckConstraint("lifecycle != 'completed' OR outcome IS NOT NULL", name="ck_run_completed_outcome"),
        UniqueConstraint("id", "user_id", name="uq_run_id_user"),
        Index("ix_run_session_created", "session_id", "created_at"),
        Index("ix_run_user_lifecycle", "user_id", "lifecycle", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_kind: Mapped[str] = mapped_column(String(32), default="normal", nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    wait_reason: Mapped[str | None] = mapped_column(String(32))
    outcome: Mapped[str | None] = mapped_column(String(32))
    entry_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    source_run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("run.id"))
    result_summary: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class WorkItem(Base, TimestampMixin, VersionMixin):
    __tablename__ = "work_item"
    __table_args__ = (
        CheckConstraint("mode IN ('chat','plan','group')", name="ck_work_item_mode"),
        CheckConstraint(
            "status IN ('queued','running','waiting_user','waiting_agent','waiting_tool','waiting_resource','succeeded','failed','timed_out','cancelled','abandoned','blocked')",
            name="ck_work_item_status",
        ),
        UniqueConstraint("id", "user_id", name="uq_work_item_id_user"),
        Index("ix_work_item_run_status", "run_id", "status"),
        Index("ix_work_item_status_updated", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    owner_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    agent_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_version.id"), nullable=False)
    parent_work_item_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("work_item.id"))
    plan_node_id: Mapped[str | None] = mapped_column(String(36))
    group_formation_proposal_id: Mapped[str | None] = mapped_column(String(36))
    group_formation_work_item_id: Mapped[str | None] = mapped_column(String(36))
    group_run_id: Mapped[str | None] = mapped_column(String(36))
    group_node_id: Mapped[str | None] = mapped_column(String(36))
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
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
        Index("ix_runner_v1_lease", "status", "lease_expires_at"),
        Index("ix_runner_v1_session_created", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    work_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("work_item.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    agent_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_version.id"), nullable=False)
    execution_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(191))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    principal_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    assembly_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    prompt_manifest: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result_message_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("message.id"))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)


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
    last_completed_step: Mapped[str | None] = mapped_column(String(100))
    safe_to_resume: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


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
    group_node_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("group_agent_node.id"))
    asked_by_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    target_user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    answer_message_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("message.id"))
    answer_text: Mapped[str | None] = mapped_column(Text)
    answered_by_user_id: Mapped[str | None] = mapped_column(String(191), ForeignKey("user_identity.user_id"))
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)


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
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
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
        CheckConstraint(
            "status IN ('created','waiting_confirmation','waiting_resource','queued','running','succeeded','failed','rejected','cancel_requested','cancelled','outcome_unknown')",
            name="ck_tool_invocation_status",
        ),
        Index("ix_tool_invocation_status_updated", "status", "updated_at"),
        Index("ix_tool_invocation_tool_status", "platform_tool_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_v1.id"), nullable=False)
    work_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("work_item.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    platform_tool_id: Mapped[str] = mapped_column(String(36), ForeignKey("platform_tool.id"), nullable=False)
    tool_key: Mapped[str] = mapped_column(String(191), nullable=False)
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
        UniqueConstraint("caller_work_item_id", "idempotency_key", name="uq_agent_invocation_business_key"),
        UniqueConstraint("target_work_item_id", name="uq_agent_invocation_target_work"),
        Index("ix_agent_invocation_caller_batch_status", "caller_work_item_id", "batch_key", "status"),
        Index("ix_agent_invocation_target_work", "target_work_item_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    caller_work_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("work_item.id"), nullable=False)
    caller_runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_v1.id"), nullable=False)
    target_work_item_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("work_item.id"))
    target_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    target_agent_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_version.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    context_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="created", nullable=False)
    batch_key: Mapped[str | None] = mapped_column(String(100))
    result_summary: Mapped[str | None] = mapped_column(Text)
    result_data: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)


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
    implementation_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class RunnerPolicySnapshot(Base):
    __tablename__ = "runner_policy_snapshot"
    __table_args__ = (UniqueConstraint("runner_id", "policy_type", name="uq_runner_policy_type"),)
    runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runner_v1.id"), primary_key=True)
    policy_definition_id: Mapped[str] = mapped_column(String(36), ForeignKey("policy_definition.id"), primary_key=True)
    parameter_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_type: Mapped[str] = mapped_column(String(32), nullable=False)
    parameters_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
