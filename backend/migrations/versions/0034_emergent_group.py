"""Persist automatic emergent Group formation mode.

Revision ID: 0034
Revises: 0033
"""

from alembic import op
import sqlalchemy as sa


revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_session",
        sa.Column(
            "formation_mode",
            sa.String(length=32),
            nullable=False,
            server_default="manual_selected",
        ),
    )
    op.create_check_constraint(
        "ck_group_session_formation_mode",
        "group_session",
        "formation_mode IN ('manual_selected','emergent_model_selected')",
    )
    op.add_column(
        "history_summary",
        sa.Column("group_run_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_history_summary_group_run_id",
        "history_summary",
        "group_run",
        ["group_run_id"],
        ["id"],
    )
    # Existing summaries predate Run-scoped history. Preserve them as legacy
    # caches and recover the owning GroupRun from their terminal source row.
    op.execute(sa.text(
        "UPDATE history_summary h JOIN message m ON m.id=h.through_message_id "
        "SET h.group_run_id=m.group_run_id WHERE h.scope_type='group_agent'"
    ))
    op.create_index(
        "ix_history_summary_group_run_status",
        "history_summary",
        ["group_run_id", "status", "through_event_sequence"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_history_summary_group_run_status", table_name="history_summary"
    )
    op.drop_constraint(
        "fk_history_summary_group_run_id", "history_summary", type_="foreignkey"
    )
    op.drop_column("history_summary", "group_run_id")
    op.drop_constraint(
        "ck_group_session_formation_mode", "group_session", type_="check"
    )
    op.drop_column("group_session", "formation_mode")
