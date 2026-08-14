"""Append-only Group Runtime diagnostics.

Revision ID: 0035
Revises: 0034
"""

from alembic import op
import sqlalchemy as sa


revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_diagnostic",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("group_session_id", sa.String(36), nullable=False),
        sa.Column("group_run_id", sa.String(36), nullable=False),
        sa.Column("diagnostic_code", sa.String(100), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("disposition", sa.String(16), nullable=False),
        sa.Column("source_entity_type", sa.String(64), nullable=False),
        sa.Column("source_entity_id", sa.String(36), nullable=False),
        sa.Column("source_event_id", sa.String(36)),
        sa.Column("caller_agent_id", sa.String(36)),
        sa.Column("target_agent_id", sa.String(36)),
        sa.Column("dedupe_key", sa.String(191), nullable=False),
        sa.Column("causal_fingerprint", sa.String(64), nullable=False),
        sa.Column("causal_chain", sa.JSON(), nullable=False),
        sa.Column("limit_snapshot", sa.JSON(), nullable=False),
        sa.Column("safe_details", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "category IN ('delegation_guard','progress_guard','quiescence','delivery')",
            name="ck_group_diagnostic_category",
        ),
        sa.CheckConstraint(
            "severity IN ('info','warning','error')",
            name="ck_group_diagnostic_severity",
        ),
        sa.CheckConstraint(
            "disposition IN ('observed','warned','blocked','recovered')",
            name="ck_group_diagnostic_disposition",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["user_session.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"]),
        sa.ForeignKeyConstraint(["group_session_id"], ["group_session.id"]),
        sa.ForeignKeyConstraint(["group_run_id"], ["group_run.id"]),
        sa.ForeignKeyConstraint(["source_event_id"], ["execution_event.id"]),
        sa.ForeignKeyConstraint(["caller_agent_id"], ["agent.id"]),
        sa.ForeignKeyConstraint(["target_agent_id"], ["agent.id"]),
        sa.UniqueConstraint(
            "run_id", "dedupe_key", name="uq_group_diagnostic_run_dedupe",
        ),
    )
    op.create_index(
        "ix_group_diagnostic_group_run_created",
        "group_diagnostic", ["group_run_id", "created_at", "id"],
    )
    op.create_index(
        "ix_group_diagnostic_run_code_created",
        "group_diagnostic", ["run_id", "diagnostic_code", "created_at"],
    )
    op.execute(
        "CREATE TRIGGER trg_group_diagnostic_immutable_update "
        "BEFORE UPDATE ON group_diagnostic FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'group diagnostic is append-only'"
    )
    op.execute(
        "CREATE TRIGGER trg_group_diagnostic_immutable_delete "
        "BEFORE DELETE ON group_diagnostic FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'group diagnostic is append-only'"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_group_diagnostic_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_group_diagnostic_immutable_update")
    op.drop_index(
        "ix_group_diagnostic_run_code_created", table_name="group_diagnostic",
    )
    op.drop_index(
        "ix_group_diagnostic_group_run_created", table_name="group_diagnostic",
    )
    op.drop_table("group_diagnostic")
