"""persist the OpenTelemetry Runner parent span context

Revision ID: 0022
Revises: 0021
"""

from alembic import op
import sqlalchemy as sa


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runner_trace", sa.Column("root_span_id", sa.String(16), nullable=True))
    op.execute(sa.text("UPDATE runner_trace SET root_span_id = LOWER(SUBSTRING(REPLACE(UUID(), '-', ''), 1, 16)) WHERE root_span_id IS NULL"))
    op.alter_column("runner_trace", "root_span_id", existing_type=sa.String(16), nullable=False)


def downgrade() -> None:
    op.drop_column("runner_trace", "root_span_id")
