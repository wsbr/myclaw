"""Add conversational schedule provenance and context mode.

Revision ID: 0046
Revises: 0045
"""

from alembic import op
import sqlalchemy as sa


revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_schedule",
        sa.Column(
            "context_mode", sa.String(24), nullable=False,
            server_default="new_session",
        ),
    )
    op.add_column(
        "agent_schedule", sa.Column("source_session_id", sa.String(36))
    )
    op.add_column(
        "agent_schedule",
        sa.Column("created_by_tool_invocation_id", sa.String(36)),
    )
    op.create_check_constraint(
        "ck_agent_schedule_context_mode", "agent_schedule",
        "context_mode IN ('new_session','continue_session')",
    )
    op.create_foreign_key(
        "fk_agent_schedule_source_session", "agent_schedule", "user_session",
        ["source_session_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_agent_schedule_tool_invocation", "agent_schedule", "tool_invocation",
        ["created_by_tool_invocation_id"], ["id"],
    )
    op.create_index(
        "ix_agent_schedule_source_session", "agent_schedule", ["source_session_id"]
    )
    op.create_unique_constraint(
        "uq_agent_schedule_created_by_tool_invocation", "agent_schedule",
        ["created_by_tool_invocation_id"],
    )
    op.add_column(
        "agent_schedule_fire",
        sa.Column(
            "context_mode_snapshot", sa.String(24), nullable=False,
            server_default="new_session",
        ),
    )
    op.add_column(
        "agent_schedule_fire",
        sa.Column("source_session_id_snapshot", sa.String(36)),
    )
    op.create_check_constraint(
        "ck_agent_schedule_fire_context_mode", "agent_schedule_fire",
        "context_mode_snapshot IN ('new_session','continue_session')",
    )
    op.create_foreign_key(
        "fk_agent_schedule_fire_source_session", "agent_schedule_fire", "user_session",
        ["source_session_id_snapshot"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_agent_schedule_fire_source_session", "agent_schedule_fire",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_agent_schedule_fire_context_mode", "agent_schedule_fire", type_="check"
    )
    op.drop_column("agent_schedule_fire", "source_session_id_snapshot")
    op.drop_column("agent_schedule_fire", "context_mode_snapshot")
    op.drop_constraint(
        "uq_agent_schedule_created_by_tool_invocation", "agent_schedule",
        type_="unique",
    )
    op.drop_index(
        "ix_agent_schedule_source_session", table_name="agent_schedule"
    )
    op.drop_constraint(
        "fk_agent_schedule_tool_invocation", "agent_schedule",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_agent_schedule_source_session", "agent_schedule",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_agent_schedule_context_mode", "agent_schedule", type_="check"
    )
    op.drop_column("agent_schedule", "created_by_tool_invocation_id")
    op.drop_column("agent_schedule", "source_session_id")
    op.drop_column("agent_schedule", "context_mode")
