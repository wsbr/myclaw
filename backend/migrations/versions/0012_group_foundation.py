"""relationship-backed Group Formation and Group runtime

Revision ID: 0012
Revises: 0011
"""

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    ]


def upgrade() -> None:
    op.create_table(
        "group_session",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("parent_session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("frozen_formation_revision_id", sa.String(36), unique=True),
        sa.Column("entry_agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="forming"),
        sa.Column("goal", sa.Text()), sa.Column("bootstrap_context", sa.Text()),
        sa.Column("confirmed_at", sa.DateTime()), sa.Column("closed_at", sa.DateTime()),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"), *timestamps(),
        sa.CheckConstraint("status IN ('forming','open','closed','archived')", name="ck_group_session_status"),
    )
    op.create_index("ix_group_session_parent_status", "group_session", ["parent_session_id", "status", "created_at"])
    op.create_table(
        "group_formation_proposal",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("parent_session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("group_session_id", sa.String(36), sa.ForeignKey("group_session.id"), unique=True, nullable=False),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("entry_agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("generation_run_id", sa.String(36), sa.ForeignKey("run.id"), unique=True, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="generating"),
        sa.Column("current_revision_id", sa.String(36)),
        sa.Column("current_revision_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decided_at", sa.DateTime()), sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *timestamps(),
        sa.CheckConstraint("status IN ('generating','awaiting_confirmation','confirmed','rejected','cancelled','failed')", name="ck_group_formation_status"),
    )
    op.create_index("ix_group_formation_parent_status", "group_formation_proposal", ["parent_session_id", "status", "created_at"])
    op.create_table(
        "group_formation_revision",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("proposal_id", sa.String(36), sa.ForeignKey("group_formation_proposal.id"), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False), sa.Column("source", sa.String(20), nullable=False),
        sa.Column("derived_from_revision_id", sa.String(36), sa.ForeignKey("group_formation_revision.id")),
        sa.Column("goal", sa.Text(), nullable=False), sa.Column("bootstrap_context", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("edited_by_type", sa.String(20), nullable=False), sa.Column("edited_by_id", sa.String(191), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("proposal_id", "revision_no", name="uq_group_formation_revision_no"),
        sa.UniqueConstraint("id", "proposal_id", name="uq_group_formation_revision_proposal"),
    )
    op.create_table(
        "group_formation_participant",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("group_formation_revision.id"), nullable=False),
        sa.Column("participant_key", sa.String(100), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("is_entry", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("initial_responsibility", sa.Text(), nullable=False), sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("revision_id", "participant_key", name="uq_group_participant_key"),
        sa.UniqueConstraint("revision_id", "agent_id", name="uq_group_participant_agent"),
    )
    op.create_table(
        "group_formation_work_item",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("group_formation_revision.id"), nullable=False),
        sa.Column("work_item_key", sa.String(100), nullable=False),
        sa.Column("participant_id", sa.String(36), sa.ForeignKey("group_formation_participant.id"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False), sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("acceptance_criteria", sa.Text(), nullable=False), sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("revision_id", "work_item_key", name="uq_group_formation_work_key"),
    )
    op.create_table(
        "group_formation_suggested_edge",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("group_formation_revision.id"), nullable=False),
        sa.Column("source_participant_id", sa.String(36), sa.ForeignKey("group_formation_participant.id"), nullable=False),
        sa.Column("target_participant_id", sa.String(36), sa.ForeignKey("group_formation_participant.id"), nullable=False),
        sa.Column("label", sa.String(300), nullable=False), sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("revision_id", "source_participant_id", "target_participant_id", name="uq_group_formation_edge"),
        sa.CheckConstraint("source_participant_id != target_participant_id", name="ck_group_formation_edge_not_self"),
    )
    op.create_table(
        "group_agent_node",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("group_session_id", sa.String(36), sa.ForeignKey("group_session.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("source_participant_id", sa.String(36), sa.ForeignKey("group_formation_participant.id"), unique=True, nullable=False),
        sa.Column("is_entry", sa.Boolean(), nullable=False), sa.Column("initial_responsibility", sa.Text(), nullable=False),
        sa.Column("availability", sa.String(20), nullable=False, server_default="available"),
        sa.Column("unavailable_reason", sa.String(300)), *timestamps(),
        sa.UniqueConstraint("group_session_id", "agent_id", name="uq_group_node_agent"),
    )
    op.create_table(
        "group_initial_suggested_edge",
        sa.Column("group_session_id", sa.String(36), sa.ForeignKey("group_session.id"), primary_key=True),
        sa.Column("source_node_id", sa.String(36), sa.ForeignKey("group_agent_node.id"), primary_key=True),
        sa.Column("target_node_id", sa.String(36), sa.ForeignKey("group_agent_node.id"), primary_key=True),
        sa.Column("label", sa.String(300), nullable=False), sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("source_edge_id", sa.String(36), sa.ForeignKey("group_formation_suggested_edge.id"), unique=True, nullable=False),
    )
    op.create_table(
        "group_run",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), unique=True, nullable=False),
        sa.Column("group_session_id", sa.String(36), sa.ForeignKey("group_session.id"), nullable=False),
        sa.Column("owner_node_id", sa.String(36), sa.ForeignKey("group_agent_node.id"), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False), sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"), *timestamps(),
    )
    op.create_index("ix_group_run_session_created", "group_run", ["group_session_id", "created_at"])
    op.create_table(
        "group_run_agent_version",
        sa.Column("group_run_id", sa.String(36), sa.ForeignKey("group_run.id"), primary_key=True),
        sa.Column("group_session_id", sa.String(36), sa.ForeignKey("group_session.id"), nullable=False),
        sa.Column("group_node_id", sa.String(36), sa.ForeignKey("group_agent_node.id"), primary_key=True),
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id")),
        sa.Column("participation_status", sa.String(20), nullable=False), sa.Column("unavailable_reason", sa.String(300)),
    )
    op.create_table(
        "group_collaboration_edge",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("group_session_id", sa.String(36), sa.ForeignKey("group_session.id"), nullable=False),
        sa.Column("source_node_id", sa.String(36), sa.ForeignKey("group_agent_node.id"), nullable=False),
        sa.Column("target_node_id", sa.String(36), sa.ForeignKey("group_agent_node.id"), nullable=False),
        sa.Column("first_invocation_id", sa.String(36), sa.ForeignKey("agent_invocation.id"), unique=True, nullable=False),
        sa.Column("created_in_group_run_id", sa.String(36), sa.ForeignKey("group_run.id"), nullable=False), *timestamps(),
        sa.UniqueConstraint("group_session_id", "source_node_id", "target_node_id", name="uq_group_actual_edge"),
    )
    op.add_column("work_item", sa.Column("group_formation_proposal_id", sa.String(36)))
    op.add_column("work_item", sa.Column("group_formation_work_item_id", sa.String(36)))
    op.create_foreign_key("fk_work_group_formation", "work_item", "group_formation_proposal", ["group_formation_proposal_id"], ["id"])
    op.create_foreign_key("fk_work_group_formation_item", "work_item", "group_formation_work_item", ["group_formation_work_item_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_work_group_formation_item", "work_item", type_="foreignkey")
    op.drop_constraint("fk_work_group_formation", "work_item", type_="foreignkey")
    op.drop_column("work_item", "group_formation_work_item_id")
    op.drop_column("work_item", "group_formation_proposal_id")
    for table in ("group_collaboration_edge", "group_run_agent_version", "group_run", "group_initial_suggested_edge",
                  "group_agent_node", "group_formation_suggested_edge", "group_formation_work_item",
                  "group_formation_participant", "group_formation_revision", "group_formation_proposal", "group_session"):
        op.drop_table(table)
