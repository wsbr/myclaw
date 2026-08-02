"""Group follow-up references and frozen graph integrity

Revision ID: 0015
Revises: 0014
"""

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def immutable_trigger(table: str) -> None:
    op.execute(sa.text(f"""
        CREATE TRIGGER trg_{table}_immutable_update
        BEFORE UPDATE ON {table} FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is immutable'
    """))
    op.execute(sa.text(f"""
        CREATE TRIGGER trg_{table}_immutable_delete
        BEFORE DELETE ON {table} FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is immutable'
    """))


def upgrade() -> None:
    op.add_column("group_agent_node", sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"))
    op.create_table(
        "group_run_reference",
        sa.Column("group_run_id", sa.String(36), sa.ForeignKey("group_run.id"), primary_key=True),
        sa.Column("referenced_group_run_id", sa.String(36), sa.ForeignKey("group_run.id"), primary_key=True),
        sa.CheckConstraint("group_run_id != referenced_group_run_id", name="ck_group_run_reference_not_self"),
    )
    op.create_check_constraint(
        "ck_group_run_agent_version_availability", "group_run_agent_version",
        "(participation_status = 'available' AND agent_version_id IS NOT NULL AND unavailable_reason IS NULL) OR "
        "(participation_status = 'unavailable' AND agent_version_id IS NULL AND unavailable_reason IS NOT NULL)",
    )
    op.create_unique_constraint("uq_group_node_id_session", "group_agent_node", ["id", "group_session_id"])
    op.create_unique_constraint("uq_group_run_id_session", "group_run", ["id", "group_session_id"])
    op.create_foreign_key(
        "fk_group_run_mapping_run_session", "group_run_agent_version", "group_run",
        ["group_run_id", "group_session_id"], ["id", "group_session_id"],
    )
    op.create_foreign_key(
        "fk_group_run_mapping_node_session", "group_run_agent_version", "group_agent_node",
        ["group_node_id", "group_session_id"], ["id", "group_session_id"],
    )
    for table in (
        "group_formation_revision", "group_formation_participant", "group_formation_work_item",
        "group_formation_suggested_edge", "group_initial_suggested_edge",
        "group_run", "group_run_agent_version", "group_run_reference",
    ):
        immutable_trigger(table)
    op.execute(sa.text("""
        CREATE TRIGGER trg_group_agent_node_frozen_update
        BEFORE UPDATE ON group_agent_node FOR EACH ROW
        BEGIN
          IF NOT (
            NEW.id <=> OLD.id AND NEW.group_session_id <=> OLD.group_session_id
            AND NEW.agent_id <=> OLD.agent_id AND NEW.source_participant_id <=> OLD.source_participant_id
            AND NEW.is_entry <=> OLD.is_entry AND NEW.initial_responsibility <=> OLD.initial_responsibility
            AND NEW.created_at <=> OLD.created_at
          ) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Group Agent node definition is frozen';
          END IF;
        END
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_group_agent_node_frozen_delete
        BEFORE DELETE ON group_agent_node FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Group Agent node is frozen'
    """))


def downgrade() -> None:
    for table in (
        "group_formation_revision", "group_formation_participant", "group_formation_work_item",
        "group_formation_suggested_edge", "group_initial_suggested_edge",
        "group_run", "group_run_agent_version", "group_run_reference",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_delete"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_update"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_group_agent_node_frozen_delete"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_group_agent_node_frozen_update"))
    op.drop_constraint("fk_group_run_mapping_node_session", "group_run_agent_version", type_="foreignkey")
    op.drop_constraint("fk_group_run_mapping_run_session", "group_run_agent_version", type_="foreignkey")
    op.drop_constraint("uq_group_run_id_session", "group_run", type_="unique")
    op.drop_constraint("uq_group_node_id_session", "group_agent_node", type_="unique")
    op.drop_constraint("ck_group_run_agent_version_availability", "group_run_agent_version", type_="check")
    op.drop_table("group_run_reference")
    op.drop_column("group_agent_node", "version")
