"""durable Clarification wait and answer state

Revision ID: 0013
Revises: 0012
"""

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clarification",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("group_session_id", sa.String(36), sa.ForeignKey("group_session.id")),
        sa.Column("group_node_id", sa.String(36), sa.ForeignKey("group_agent_node.id")),
        sa.Column("asked_by_agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("target_user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("answer_message_id", sa.String(36), sa.ForeignKey("message.id")),
        sa.Column("answer_text", sa.Text()),
        sa.Column("answered_by_user_id", sa.String(191), sa.ForeignKey("user_identity.user_id")),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("status IN ('open','answered','cancelled')", name="ck_clarification_status"),
        sa.UniqueConstraint("work_item_id", "idempotency_key", name="uq_clarification_work_key"),
    )
    op.create_index(
        "ix_clarification_target_status_created", "clarification",
        ["target_user_id", "status", "created_at"],
    )
    op.create_foreign_key(
        "fk_message_clarification", "message", "clarification", ["clarification_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_message_clarification", "message", type_="foreignkey")
    op.drop_index("ix_clarification_target_status_created", table_name="clarification")
    op.drop_table("clarification")
