"""durable directed AgentInvocation

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_invocation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("caller_work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("caller_runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), nullable=False),
        sa.Column("target_work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), unique=True),
        sa.Column("target_agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("target_agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id"), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("context_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="created"),
        sa.Column("batch_key", sa.String(100)),
        sa.Column("result_summary", sa.Text()), sa.Column("result_data", sa.JSON()),
        sa.Column("error_code", sa.String(100)), sa.Column("error_detail", sa.Text()),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "status IN ('created','queued','running','succeeded','failed','cancelled','timed_out','rejected')",
            name="ck_agent_invocation_status",
        ),
        sa.UniqueConstraint("caller_work_item_id", "idempotency_key", name="uq_agent_invocation_business_key"),
    )
    op.create_index("ix_agent_invocation_caller_batch_status", "agent_invocation",
                    ["caller_work_item_id", "batch_key", "status"])
    op.create_index("ix_agent_invocation_target_work", "agent_invocation", ["target_work_item_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_invocation_target_work", table_name="agent_invocation")
    op.drop_index("ix_agent_invocation_caller_batch_status", table_name="agent_invocation")
    op.drop_table("agent_invocation")
