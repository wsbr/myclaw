"""Persist process-level worker heartbeat and slot capacity.

Revision ID: 0040
Revises: 0039
"""

from alembic import op
import sqlalchemy as sa


revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_instance",
        sa.Column("worker_id", sa.String(191), primary_key=True),
        sa.Column("worker_kind", sa.String(32), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("active_slots", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("process_id", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.String(191), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("stopped_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("capacity > 0", name="ck_worker_instance_capacity_positive"),
        sa.CheckConstraint(
            "active_slots >= 0 AND active_slots <= capacity",
            name="ck_worker_instance_active_slots",
        ),
        sa.CheckConstraint(
            "status IN ('active','stopped')", name="ck_worker_instance_status",
        ),
    )
    op.create_index(
        "ix_worker_instance_kind_heartbeat", "worker_instance",
        ["worker_kind", "heartbeat_at"],
    )


def downgrade() -> None:
    op.drop_table("worker_instance")
