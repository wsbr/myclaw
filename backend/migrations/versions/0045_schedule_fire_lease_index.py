"""Add a targeted schedule dispatch lease index.

Revision ID: 0045
Revises: 0044
"""

from alembic import op


revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_agent_schedule_fire_lease",
        "agent_schedule_fire",
        ["status", "lease_expires_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_schedule_fire_lease",
        table_name="agent_schedule_fire",
    )
