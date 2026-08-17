"""Add durable Agent schedules, fire ledger, and Runner snapshots.

Revision ID: 0044
Revises: 0043
"""

from alembic import op
import sqlalchemy as sa


revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_schedule",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(191), nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("schedule_type", sa.String(20), nullable=False),
        sa.Column("cron_expression", sa.String(191)),
        sa.Column("interval_seconds", sa.BigInteger()),
        sa.Column("run_at", sa.DateTime()),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("next_fire_at", sa.DateTime()),
        sa.Column("end_at", sa.DateTime()),
        sa.Column("version_policy", sa.String(32), nullable=False),
        sa.Column("pinned_agent_version_id", sa.String(36)),
        sa.Column("overlap_policy", sa.String(20), nullable=False),
        sa.Column("misfire_policy", sa.String(20), nullable=False),
        sa.Column("misfire_grace_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("total_fire_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_scheduled_for", sa.DateTime()),
        sa.Column("last_fired_at", sa.DateTime()),
        sa.Column("last_error_code", sa.String(100)),
        sa.Column("last_error_detail", sa.Text()),
        sa.Column("lease_owner", sa.String(191)),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["platform_account.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user_identity.user_id"]),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
        sa.ForeignKeyConstraint(["pinned_agent_version_id"], ["agent_version.id"]),
        sa.CheckConstraint("schedule_type IN ('once','cron','interval')", name="ck_agent_schedule_type"),
        sa.CheckConstraint("status IN ('active','paused','completed','disabled')", name="ck_agent_schedule_status"),
        sa.CheckConstraint("version_policy IN ('latest_published','pinned')", name="ck_agent_schedule_version_policy"),
        sa.CheckConstraint("overlap_policy IN ('skip','queue_one','allow')", name="ck_agent_schedule_overlap_policy"),
        sa.CheckConstraint("misfire_policy IN ('skip','fire_once')", name="ck_agent_schedule_misfire_policy"),
        sa.CheckConstraint("misfire_grace_seconds >= 0", name="ck_agent_schedule_misfire_grace"),
        sa.CheckConstraint("total_fire_count >= 0", name="ck_agent_schedule_fire_count"),
        sa.CheckConstraint(
            "(schedule_type = 'once' AND run_at IS NOT NULL AND cron_expression IS NULL AND interval_seconds IS NULL) OR "
            "(schedule_type = 'cron' AND run_at IS NULL AND cron_expression IS NOT NULL AND interval_seconds IS NULL) OR "
            "(schedule_type = 'interval' AND run_at IS NULL AND cron_expression IS NULL AND interval_seconds IS NOT NULL)",
            name="ck_agent_schedule_shape",
        ),
        sa.CheckConstraint(
            "(version_policy = 'pinned' AND pinned_agent_version_id IS NOT NULL) OR "
            "(version_policy = 'latest_published' AND pinned_agent_version_id IS NULL)",
            name="ck_agent_schedule_version_shape",
        ),
    )
    op.create_index("ix_agent_schedule_due", "agent_schedule", ["status", "next_fire_at", "id"])
    op.create_index("ix_agent_schedule_owner", "agent_schedule", ["tenant_id", "account_id", "status", "updated_at"])
    op.create_index("ix_agent_schedule_agent", "agent_schedule", ["agent_id", "status", "next_fire_at"])
    op.create_index("ix_agent_schedule_lease", "agent_schedule", ["lease_expires_at", "lease_owner"])

    op.drop_constraint("ck_user_session_origin", "user_session", type_="check")
    op.create_check_constraint(
        "ck_user_session_origin", "user_session",
        "origin_type IN ('web','api','channel','schedule')",
    )
    op.add_column("user_session", sa.Column("origin_schedule_id", sa.String(36)))
    op.create_foreign_key(
        "fk_user_session_origin_schedule", "user_session", "agent_schedule",
        ["origin_schedule_id"], ["id"],
    )
    op.create_index("ix_user_session_origin_schedule", "user_session", ["origin_schedule_id"])

    op.create_table(
        "agent_schedule_fire",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("schedule_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("trigger_kind", sa.String(20), nullable=False),
        sa.Column("operation_key", sa.String(191), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason_code", sa.String(100)),
        sa.Column("schedule_version", sa.BigInteger(), nullable=False),
        sa.Column("prompt_snapshot", sa.Text(), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("timezone_snapshot", sa.String(100), nullable=False),
        sa.Column("version_policy_snapshot", sa.String(32), nullable=False),
        sa.Column("agent_version_id", sa.String(36)),
        sa.Column("session_id", sa.String(36)),
        sa.Column("input_message_id", sa.String(36)),
        sa.Column("run_id", sa.String(36)),
        sa.Column("runner_id", sa.String(36)),
        sa.Column("fired_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("notification_read_at", sa.DateTime()),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("lease_owner", sa.String(191)),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["schedule_id"], ["agent_schedule.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["platform_account.id"]),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_version.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["user_session.id"]),
        sa.ForeignKeyConstraint(["input_message_id"], ["message.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"]),
        sa.ForeignKeyConstraint(["runner_id"], ["runner_v1.id"]),
        sa.CheckConstraint("trigger_kind IN ('scheduled','manual')", name="ck_agent_schedule_fire_trigger"),
        sa.CheckConstraint(
            "status IN ('queued','deferred','dispatching','running','succeeded','failed','skipped','cancelled')",
            name="ck_agent_schedule_fire_status",
        ),
        sa.UniqueConstraint("schedule_id", "operation_key", name="uq_agent_schedule_fire_operation"),
        sa.UniqueConstraint("runner_id", name="uq_agent_schedule_fire_runner"),
        sa.UniqueConstraint("run_id", name="uq_agent_schedule_fire_run"),
    )
    op.create_index("ix_agent_schedule_fire_claim", "agent_schedule_fire", ["status", "scheduled_for", "created_at"])
    op.create_index("ix_agent_schedule_fire_schedule", "agent_schedule_fire", ["schedule_id", "scheduled_for", "id"])
    op.create_index("ix_agent_schedule_fire_owner", "agent_schedule_fire", ["tenant_id", "account_id", "status", "created_at"])
    op.create_index("ix_agent_schedule_fire_unread", "agent_schedule_fire", ["account_id", "notification_read_at", "completed_at"])

    op.create_table(
        "runner_schedule_snapshot",
        sa.Column("runner_id", sa.String(36), primary_key=True),
        sa.Column("fire_id", sa.String(36), nullable=False),
        sa.Column("schedule_id", sa.String(36), nullable=False),
        sa.Column("schedule_version", sa.BigInteger(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("agent_version_id", sa.String(36), nullable=False),
        sa.Column("account_auth_revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["runner_id"], ["runner_v1.id"]),
        sa.ForeignKeyConstraint(["fire_id"], ["agent_schedule_fire.id"]),
        sa.ForeignKeyConstraint(["schedule_id"], ["agent_schedule.id"]),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_version.id"]),
        sa.UniqueConstraint("fire_id", name="uq_runner_schedule_snapshot_fire"),
    )
    op.create_index("ix_runner_schedule_snapshot_schedule", "runner_schedule_snapshot", ["schedule_id", "scheduled_for"])


def downgrade() -> None:
    op.drop_table("runner_schedule_snapshot")
    op.drop_table("agent_schedule_fire")
    op.drop_index("ix_user_session_origin_schedule", table_name="user_session")
    op.drop_constraint("fk_user_session_origin_schedule", "user_session", type_="foreignkey")
    op.drop_column("user_session", "origin_schedule_id")
    op.drop_constraint("ck_user_session_origin", "user_session", type_="check")
    op.create_check_constraint(
        "ck_user_session_origin", "user_session",
        "origin_type IN ('web','api','channel')",
    )
    op.drop_table("agent_schedule")
