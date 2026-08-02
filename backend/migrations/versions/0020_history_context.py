"""Visibility-scoped history summaries and frozen policy parameters.

Revision ID: 0020
Revises: 0019
"""

from alembic import op
import sqlalchemy as sa


revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runner_policy_snapshot", sa.Column("parameters_snapshot", sa.JSON(), nullable=True))
    op.execute(sa.text("UPDATE runner_policy_snapshot SET parameters_snapshot = JSON_OBJECT() WHERE parameters_snapshot IS NULL"))
    op.alter_column("runner_policy_snapshot", "parameters_snapshot", existing_type=sa.JSON(), nullable=False)
    op.create_table(
        "history_summary",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("scope_type", sa.String(20), nullable=False),
        sa.Column("group_session_id", sa.String(36), sa.ForeignKey("group_session.id")),
        sa.Column("group_node_id", sa.String(36), sa.ForeignKey("group_agent_node.id")),
        sa.Column("scope_discriminator", sa.String(191), sa.Computed(
            "CASE WHEN scope_type = 'chat' THEN 'chat' ELSE CONCAT('group_node:', group_node_id) END",
            persisted=True,
        ), nullable=False),
        sa.Column("through_message_id", sa.String(36), sa.ForeignKey("message.id"), nullable=False),
        sa.Column("through_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("model_id", sa.String(36), sa.ForeignKey("model.id"), nullable=False),
        sa.Column("parameter_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("scope_type IN ('chat','group_agent')", name="ck_history_summary_scope"),
        sa.CheckConstraint("status IN ('pending','ready','failed','superseded')", name="ck_history_summary_status"),
        sa.CheckConstraint(
            "(scope_type = 'chat' AND group_session_id IS NULL AND group_node_id IS NULL) OR "
            "(scope_type = 'group_agent' AND group_session_id IS NOT NULL AND group_node_id IS NOT NULL)",
            name="ck_history_summary_scope_fields",
        ),
        sa.UniqueConstraint(
            "session_id", "scope_discriminator", "through_event_sequence",
            name="uq_history_summary_scope_sequence",
        ),
    )
    op.create_index(
        "ix_history_summary_ready", "history_summary",
        ["session_id", "scope_discriminator", "status", "through_event_sequence"],
    )


def downgrade():
    op.drop_table("history_summary")
    op.drop_column("runner_policy_snapshot", "parameters_snapshot")
