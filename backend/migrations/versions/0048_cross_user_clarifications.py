"""Add structured mentions and cross-user Clarification routing.

Revision ID: 0048
Revises: 0047
"""

from alembic import op
import sqlalchemy as sa


revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_mention",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("target_account_id", sa.String(36), nullable=False),
        sa.Column("display_name_snapshot", sa.String(200), nullable=False),
        sa.Column("handle_snapshot", sa.String(191), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["message.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["target_account_id"], ["platform_account.id"]),
        sa.UniqueConstraint(
            "message_id", "target_account_id", name="uq_message_mention_account",
        ),
    )
    op.create_index(
        "ix_message_mention_target_created", "message_mention",
        ["target_account_id", "created_at", "message_id"],
    )
    op.add_column("clarification", sa.Column("tenant_id", sa.String(36)))
    op.add_column("clarification", sa.Column("requester_account_id", sa.String(36)))
    op.add_column("clarification", sa.Column("target_account_id", sa.String(36)))
    op.add_column(
        "clarification", sa.Column("target_display_name_snapshot", sa.String(200)),
    )
    op.add_column(
        "clarification", sa.Column("target_handle_snapshot", sa.String(191)),
    )
    op.add_column("clarification", sa.Column("context_summary", sa.Text()))
    op.create_foreign_key(
        "fk_clarification_tenant", "clarification", "tenant",
        ["tenant_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_clarification_requester_account", "clarification", "platform_account",
        ["requester_account_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_clarification_target_account", "clarification", "platform_account",
        ["target_account_id"], ["id"],
    )
    op.create_index(
        "ix_clarification_target_account_status_created", "clarification",
        ["target_account_id", "status", "created_at"],
    )
    connection = op.get_bind()
    rows = connection.execute(sa.text(
        "SELECT c.id, s.tenant_id, s.account_id "
        "FROM clarification c JOIN user_session s ON s.id = c.session_id "
        "WHERE c.target_account_id IS NULL"
    )).mappings()
    for row in rows:
        connection.execute(sa.text(
            "UPDATE clarification SET tenant_id=:tenant_id, "
            "requester_account_id=:account_id, target_account_id=:account_id "
            "WHERE id=:id"
        ), dict(row))


def downgrade() -> None:
    op.drop_index(
        "ix_clarification_target_account_status_created", table_name="clarification",
    )
    op.drop_constraint(
        "fk_clarification_target_account", "clarification", type_="foreignkey",
    )
    op.drop_constraint(
        "fk_clarification_requester_account", "clarification", type_="foreignkey",
    )
    op.drop_constraint(
        "fk_clarification_tenant", "clarification", type_="foreignkey",
    )
    op.drop_column("clarification", "context_summary")
    op.drop_column("clarification", "target_handle_snapshot")
    op.drop_column("clarification", "target_display_name_snapshot")
    op.drop_column("clarification", "target_account_id")
    op.drop_column("clarification", "requester_account_id")
    op.drop_column("clarification", "tenant_id")
    op.drop_table("message_mention")
