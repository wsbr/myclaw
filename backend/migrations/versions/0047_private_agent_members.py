"""Add revocable member grants for private Agents.

Revision ID: 0047
Revises: 0046
"""

from alembic import op
import sqlalchemy as sa


revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_member",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("granted_by_account_id", sa.String(36), nullable=False),
        sa.Column("granted_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["agent_id", "tenant_id"], ["agent.id", "agent.tenant_id"],
            name="fk_agent_member_agent_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "tenant_id"],
            ["platform_account.id", "platform_account.tenant_id"],
            name="fk_agent_member_account_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_account_id", "tenant_id"],
            ["platform_account.id", "platform_account.tenant_id"],
            name="fk_agent_member_grantor_tenant",
        ),
        sa.CheckConstraint(
            "status IN ('active','revoked')", name="ck_agent_member_status",
        ),
        sa.UniqueConstraint(
            "agent_id", "account_id", name="uq_agent_member_account",
        ),
    )
    op.create_index(
        "ix_agent_member_account_status", "agent_member",
        ["account_id", "status", "agent_id"],
    )
    op.create_index(
        "ix_agent_member_agent_status", "agent_member",
        ["agent_id", "status", "account_id"],
    )


def downgrade() -> None:
    op.drop_table("agent_member")
