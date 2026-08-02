"""relationship-backed Runner assembly snapshots

Revision ID: 0009
Revises: 0008
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runner_skill_snapshot",
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), primary_key=True),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("skill.id"), primary_key=True),
        sa.Column("skill_version_id", sa.String(36), sa.ForeignKey("skill_version.id"), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("path_uri", sa.String(1000), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
    )
    op.create_table(
        "runner_tool_snapshot",
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), primary_key=True),
        sa.Column("platform_tool_id", sa.String(36), sa.ForeignKey("platform_tool.id"), primary_key=True),
        sa.Column("implementation_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.BigInteger(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
    )
    op.create_table(
        "runner_policy_snapshot",
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), primary_key=True),
        sa.Column("policy_definition_id", sa.String(36), sa.ForeignKey("policy_definition.id"), primary_key=True),
        sa.Column("parameter_version", sa.Integer(), nullable=False),
        sa.Column("policy_type", sa.String(32), nullable=False),
        sa.UniqueConstraint("runner_id", "policy_type", name="uq_runner_policy_type"),
    )


def downgrade() -> None:
    op.drop_table("runner_policy_snapshot")
    op.drop_table("runner_tool_snapshot")
    op.drop_table("runner_skill_snapshot")
