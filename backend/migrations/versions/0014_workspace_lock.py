"""fair leased Workspace read/write lock

Revision ID: 0014
Revises: 0013
"""

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    ]


def upgrade() -> None:
    op.create_table(
        "workspace_lock",
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspace.id"), primary_key=True),
        sa.Column("writer_request_id", sa.String(36)),
        sa.Column("writer_fencing_token", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("writer_lease_expires_at", sa.DateTime()),
        sa.Column("next_ticket", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *timestamps(),
    )
    op.create_table(
        "workspace_lock_request",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspace.id"), nullable=False),
        sa.Column("tool_invocation_id", sa.String(36), sa.ForeignKey("tool_invocation.id"), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("ticket", sa.BigInteger(), nullable=False),
        sa.Column("lease_token", sa.String(36)), sa.Column("fencing_token", sa.BigInteger()),
        sa.Column("lease_expires_at", sa.DateTime()), sa.Column("granted_at", sa.DateTime()),
        sa.Column("released_at", sa.DateTime()),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *timestamps(),
        sa.CheckConstraint("mode IN ('read','write')", name="ck_workspace_lock_request_mode"),
        sa.CheckConstraint(
            "status IN ('queued','granted','released','expired')",
            name="ck_workspace_lock_request_status",
        ),
    )
    op.create_index(
        "ix_workspace_lock_request_queue", "workspace_lock_request",
        ["workspace_id", "status", "ticket"],
    )
    op.create_index(
        "ix_workspace_lock_request_tool", "workspace_lock_request",
        ["tool_invocation_id", "created_at"],
    )
    op.create_table(
        "workspace_read_lease",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspace.id"), nullable=False),
        sa.Column("request_id", sa.String(36), sa.ForeignKey("workspace_lock_request.id"), unique=True, nullable=False),
        sa.Column("ticket", sa.BigInteger(), nullable=False),
        sa.Column("lease_token", sa.String(36), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        *timestamps(),
    )
    op.create_index(
        "ix_workspace_read_lease_expiry", "workspace_read_lease",
        ["workspace_id", "expires_at"],
    )
    op.create_foreign_key(
        "fk_workspace_lock_writer_request", "workspace_lock", "workspace_lock_request",
        ["writer_request_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_workspace_lock_writer_request", "workspace_lock", type_="foreignkey")
    op.drop_table("workspace_read_lease")
    op.drop_table("workspace_lock_request")
    op.drop_table("workspace_lock")
