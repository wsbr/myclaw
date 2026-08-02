"""phase-one Agent API keys

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-02 00:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_api_key",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("caller_label", sa.String(200), nullable=False),
        sa.Column("prefix", sa.String(32), nullable=False, unique=True),
        sa.Column("secret_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_by_account_id", sa.String(36), sa.ForeignKey("platform_account.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("last_used_at", sa.DateTime()),
        sa.Column("rotated_from_id", sa.String(36), sa.ForeignKey("agent_api_key.id")),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("status IN ('active','revoked','expired')", name="ck_agent_api_key_status"),
    )
    op.create_index("ix_agent_api_key_agent_status", "agent_api_key", ["agent_id", "status"])
    op.create_index("ix_agent_api_key_expiry", "agent_api_key", ["expires_at", "status"])


def downgrade() -> None:
    op.drop_table("agent_api_key")
