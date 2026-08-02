"""durable Chat runtime core aggregates

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-02 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    ]


def upgrade() -> None:
    op.create_foreign_key("fk_user_session_entry_agent", "user_session", "agent", ["entry_agent_id"], ["id"])
    op.create_foreign_key("fk_user_session_origin_api_key", "user_session", "agent_api_key", ["origin_api_key_id"], ["id"])
    op.create_table(
        "message",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("scope_type", sa.String(20), nullable=False, server_default="chat"),
        sa.Column("group_session_id", sa.String(36)), sa.Column("group_run_id", sa.String(36)),
        sa.Column("sender_type", sa.String(20), nullable=False), sa.Column("sender_user_id", sa.String(191)),
        sa.Column("sender_agent_id", sa.String(36), sa.ForeignKey("agent.id")),
        sa.Column("recipient_type", sa.String(20), nullable=False, server_default="session"),
        sa.Column("recipient_group_node_id", sa.String(36)), sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False), sa.Column("content_format", sa.String(20), nullable=False, server_default="markdown"),
        sa.Column("status", sa.String(20), nullable=False, server_default="committed"),
        sa.Column("run_id", sa.String(36)), sa.Column("work_item_id", sa.String(36)),
        sa.Column("clarification_id", sa.String(36)),
        sa.Column("reply_to_message_id", sa.String(36), sa.ForeignKey("message.id")),
        sa.Column("caused_by_event_id", sa.String(36), sa.ForeignKey("execution_event.id")),
        *timestamps(),
        sa.CheckConstraint("scope_type IN ('chat','group')", name="ck_message_scope"),
        sa.CheckConstraint("sender_type IN ('user','agent','system')", name="ck_message_sender"),
        sa.CheckConstraint("role IN ('user','assistant','system','tool_notice')", name="ck_message_role"),
        sa.CheckConstraint("status IN ('committed','superseded','redacted')", name="ck_message_status"),
    )
    op.create_index("ix_message_session_created", "message", ["session_id", "created_at", "id"])
    op.create_index("ix_message_run_created", "message", ["run_id", "created_at"])
    op.create_table(
        "run",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False), sa.Column("execution_kind", sa.String(32), nullable=False, server_default="normal"),
        sa.Column("lifecycle", sa.String(20), nullable=False, server_default="running"), sa.Column("wait_reason", sa.String(32)),
        sa.Column("outcome", sa.String(32)), sa.Column("entry_agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("source_run_id", sa.String(36), sa.ForeignKey("run.id")), sa.Column("result_summary", sa.Text()),
        sa.Column("error_code", sa.String(100)), sa.Column("cancellation_requested_at", sa.DateTime()),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"), sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()), *timestamps(),
        sa.UniqueConstraint("id", "user_id", name="uq_run_id_user"),
        sa.CheckConstraint("mode IN ('chat','plan','group')", name="ck_run_mode"),
        sa.CheckConstraint("execution_kind IN ('normal','draft_test','proposal_generation')", name="ck_run_kind"),
        sa.CheckConstraint("lifecycle IN ('running','waiting','settling','completed','cancelled')", name="ck_run_lifecycle"),
        sa.CheckConstraint("outcome IS NULL OR outcome IN ('succeeded','partially_succeeded','failed')", name="ck_run_outcome"),
        sa.CheckConstraint("(lifecycle = 'waiting' AND wait_reason IS NOT NULL) OR (lifecycle != 'waiting' AND wait_reason IS NULL)", name="ck_run_wait_reason"),
        sa.CheckConstraint("lifecycle != 'completed' OR outcome IS NOT NULL", name="ck_run_completed_outcome"),
    )
    op.create_index("ix_run_session_created", "run", ["session_id", "created_at"])
    op.create_index("ix_run_user_lifecycle", "run", ["user_id", "lifecycle", "updated_at"])
    op.create_table(
        "work_item",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False), sa.Column("owner_agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id"), nullable=False),
        sa.Column("parent_work_item_id", sa.String(36), sa.ForeignKey("work_item.id")),
        sa.Column("plan_node_id", sa.String(36)), sa.Column("group_run_id", sa.String(36)), sa.Column("group_node_id", sa.String(36)),
        sa.Column("goal", sa.Text(), nullable=False), sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("active_runner_id", sa.String(36)), sa.Column("execution_epoch", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"), sa.Column("result_summary", sa.Text()),
        sa.Column("result_data", sa.JSON()), sa.Column("error_code", sa.String(100)), sa.Column("error_detail", sa.Text()),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *timestamps(), sa.UniqueConstraint("id", "user_id", name="uq_work_item_id_user"),
        sa.CheckConstraint("mode IN ('chat','plan','group')", name="ck_work_item_mode"),
        sa.CheckConstraint("status IN ('queued','running','waiting_user','waiting_agent','waiting_tool','waiting_resource','succeeded','failed','timed_out','cancelled','abandoned','blocked')", name="ck_work_item_status"),
    )
    op.create_index("ix_work_item_run_status", "work_item", ["run_id", "status"])
    op.create_index("ix_work_item_status_updated", "work_item", ["status", "updated_at"])
    op.create_table(
        "runner_v1",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False), sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False), sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id"), nullable=False),
        sa.Column("execution_epoch", sa.BigInteger(), nullable=False), sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("lease_owner", sa.String(191)), sa.Column("lease_token", sa.String(36)), sa.Column("lease_expires_at", sa.DateTime()), sa.Column("heartbeat_at", sa.DateTime()),
        sa.Column("principal_snapshot", sa.JSON(), nullable=False), sa.Column("assembly_snapshot", sa.JSON(), nullable=False),
        sa.Column("prompt_manifest", sa.JSON(), nullable=False), sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("result_message_id", sa.String(36), sa.ForeignKey("message.id")), sa.Column("error_code", sa.String(100)), sa.Column("error_detail", sa.Text()),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"), *timestamps(),
        sa.UniqueConstraint("work_item_id", "execution_epoch", name="uq_runner_work_epoch"),
        sa.CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled','lost')", name="ck_runner_v1_status"),
    )
    op.create_index("ix_runner_v1_lease", "runner_v1", ["status", "lease_expires_at"])
    op.create_index("ix_runner_v1_session_created", "runner_v1", ["session_id", "created_at"])
    op.create_foreign_key("fk_work_item_active_runner", "work_item", "runner_v1", ["active_runner_id"], ["id"])
    op.create_foreign_key("fk_message_run", "message", "run", ["run_id"], ["id"])
    op.create_foreign_key("fk_message_work_item", "message", "work_item", ["work_item_id"], ["id"])
    op.create_table(
        "runner_checkpoint",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), nullable=False),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), nullable=False), sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("checkpoint_type", sa.String(64), nullable=False), sa.Column("state_snapshot", sa.JSON(), nullable=False),
        sa.Column("last_completed_step", sa.String(100)), sa.Column("safe_to_resume", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("runner_id", "sequence_no", name="uq_runner_checkpoint_sequence"),
    )
    op.create_index("ix_runner_checkpoint_work_safe", "runner_checkpoint", ["work_item_id", "safe_to_resume", "created_at"])


def downgrade() -> None:
    op.drop_table("runner_checkpoint")
    op.drop_constraint("fk_message_work_item", "message", type_="foreignkey")
    op.drop_constraint("fk_message_run", "message", type_="foreignkey")
    op.drop_constraint("fk_work_item_active_runner", "work_item", type_="foreignkey")
    op.drop_table("runner_v1")
    op.drop_table("work_item")
    op.drop_table("run")
    op.drop_table("message")
    op.drop_constraint("fk_user_session_origin_api_key", "user_session", type_="foreignkey")
    op.drop_constraint("fk_user_session_entry_agent", "user_session", type_="foreignkey")
