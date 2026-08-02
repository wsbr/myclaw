"""durable Tool invocation and session Sandbox runtime

Revision ID: 0008
Revises: 0007
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    ]


def upgrade() -> None:
    op.create_table(
        "session_sandbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False, unique=True),
        sa.Column("sandbox_profile_id", sa.String(36), sa.ForeignKey("sandbox_profile.id"), nullable=False),
        sa.Column("profile_config_version", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="provisioning"),
        sa.Column("root_uri", sa.String(1000), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"), *timestamps(),
        sa.CheckConstraint("status IN ('provisioning','ready','failed','cleaned')", name="ck_session_sandbox_status"),
    )
    op.create_table(
        "workspace",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_sandbox_id", sa.String(36), sa.ForeignKey("session_sandbox.id"), nullable=False, unique=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False, unique=True),
        sa.Column("root_uri", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("retention_policy_key", sa.String(64), nullable=False, server_default="session_lifetime"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"), *timestamps(),
        sa.CheckConstraint("status IN ('active','cleaning','cleaned','failed')", name="ck_workspace_status"),
    )
    op.create_table(
        "tool_invocation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), nullable=False),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("platform_tool_id", sa.String(36), sa.ForeignKey("platform_tool.id"), nullable=False),
        sa.Column("tool_key", sa.String(191), nullable=False),
        sa.Column("execution_mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="created"),
        sa.Column("arguments", sa.JSON(), nullable=False), sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("business_idempotency_key", sa.String(191), nullable=False),
        sa.Column("principal_hash", sa.String(64), nullable=False), sa.Column("result_data", sa.JSON()),
        sa.Column("error_code", sa.String(100)), sa.Column("error_detail", sa.Text()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"), *timestamps(),
        sa.UniqueConstraint("work_item_id", "business_idempotency_key", name="uq_tool_invocation_business_key"),
        sa.CheckConstraint("status IN ('created','waiting_confirmation','waiting_resource','queued','running','succeeded','failed','rejected','cancel_requested','cancelled','outcome_unknown')", name="ck_tool_invocation_status"),
    )
    op.create_index("ix_tool_invocation_status_updated", "tool_invocation", ["status", "updated_at"])
    op.create_index("ix_tool_invocation_tool_status", "tool_invocation", ["platform_tool_id", "status"])
    op.create_table(
        "tool_attempt",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tool_invocation_id", sa.String(36), sa.ForeignKey("tool_invocation.id"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("worker_id", sa.String(191)), sa.Column("lease_token", sa.String(36)),
        sa.Column("provider_request_id", sa.String(191)), sa.Column("request_snapshot", sa.JSON(), nullable=False),
        sa.Column("response_snapshot", sa.JSON()), sa.Column("error_class", sa.String(191)),
        sa.Column("error_code", sa.String(100)), sa.Column("error_detail", sa.Text()),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()), *timestamps(),
        sa.UniqueConstraint("tool_invocation_id", "attempt_no", name="uq_tool_attempt_number"),
        sa.UniqueConstraint("tool_invocation_id", "provider_request_id", name="uq_tool_attempt_provider_request"),
        sa.CheckConstraint("status IN ('queued','running','succeeded','failed','timed_out','cancelled','outcome_unknown')", name="ck_tool_attempt_status"),
    )
    op.create_table(
        "tool_confirmation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tool_invocation_id", sa.String(36), sa.ForeignKey("tool_invocation.id"), nullable=False, unique=True),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("tool_key", sa.String(191), nullable=False), sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("principal_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("decision_by_user_id", sa.String(191)), sa.Column("decision_reason", sa.Text()),
        sa.Column("policy_version", sa.BigInteger(), nullable=False),
        sa.Column("decided_at", sa.DateTime()), sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *timestamps(),
        sa.CheckConstraint("status IN ('pending','approved','rejected','cancelled')", name="ck_tool_confirmation_status"),
    )
    op.create_index("ix_tool_confirmation_user_status", "tool_confirmation", ["user_id", "status", "created_at"])


def downgrade() -> None:
    op.drop_table("tool_confirmation")
    op.drop_table("tool_attempt")
    op.drop_table("tool_invocation")
    op.drop_table("workspace")
    op.drop_table("session_sandbox")
