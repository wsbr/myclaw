"""Add Bailian as a native Search Provider connection.

Revision ID: 0033
Revises: 0032
"""

from alembic import op
import sqlalchemy as sa


revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_tool_connection_type", "tool_connection", type_="check")
    op.create_check_constraint(
        "ck_tool_connection_type",
        "tool_connection",
        "connection_type IN ('bocha','brave','bailian','mcp','browser')",
    )


def downgrade() -> None:
    bailian_rows = op.get_bind().scalar(sa.text(
        "SELECT COUNT(*) FROM tool_connection WHERE connection_type = 'bailian'"
    ))
    if bailian_rows:
        raise RuntimeError(
            "cannot downgrade 0033 while Bailian Tool connections exist"
        )
    op.drop_constraint("ck_tool_connection_type", "tool_connection", type_="check")
    op.create_check_constraint(
        "ck_tool_connection_type",
        "tool_connection",
        "connection_type IN ('bocha','brave','mcp','browser')",
    )
