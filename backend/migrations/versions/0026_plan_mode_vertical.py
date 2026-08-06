"""phase-one Plan permission-mode vertical loop

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_session",
        sa.Column("interaction_mode", sa.String(16), nullable=False, server_default="chat"),
    )
    op.add_column(
        "user_session",
        sa.Column("interaction_revision", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_user_session_interaction_mode",
        "user_session",
        "interaction_mode IN ('chat','plan')",
    )

    op.add_column(
        "approval",
        sa.Column("subject_snapshot", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())")),
    )

    op.add_column("agent_invocation", sa.Column("run_id", sa.String(36)))
    op.add_column("agent_invocation", sa.Column("caller_agent_id", sa.String(36)))
    op.add_column(
        "agent_invocation",
        sa.Column("return_route", sa.String(20), nullable=False, server_default="caller"),
    )
    op.add_column("agent_invocation", sa.Column("operation_key", sa.String(191)))
    op.add_column("agent_invocation", sa.Column("request_hash", sa.String(64)))
    op.add_column(
        "agent_invocation",
        sa.Column("authorization_snapshot", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())")),
    )
    op.add_column("agent_invocation", sa.Column("completed_at", sa.DateTime()))
    op.execute(
        "UPDATE agent_invocation ai "
        "JOIN work_item wi ON wi.id=ai.caller_work_item_id "
        "JOIN runner_v1 r ON r.id=ai.caller_runner_id "
        "SET ai.run_id=wi.run_id, ai.caller_agent_id=r.agent_id, "
        "ai.operation_key=ai.idempotency_key"
    )
    op.execute(
        "UPDATE agent_invocation SET request_hash="
        "SHA2(CONCAT(target_agent_id, '\\n', request_text), 256)"
    )
    for column, existing_type in (
        ("run_id", sa.String(36)),
        ("caller_agent_id", sa.String(36)),
        ("operation_key", sa.String(191)),
        ("request_hash", sa.String(64)),
    ):
        op.alter_column(
            "agent_invocation", column,
            existing_type=existing_type, nullable=False,
        )
    op.add_column("agent_invocation", sa.Column("result_message_id", sa.String(36)))
    op.create_foreign_key(
        "fk_agent_invocation_run", "agent_invocation", "run", ["run_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_agent_invocation_result_message", "agent_invocation", "message",
        ["result_message_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_agent_invocation_caller_agent", "agent_invocation", "agent", ["caller_agent_id"], ["id"]
    )
    op.create_check_constraint(
        "ck_agent_invocation_return_route",
        "agent_invocation",
        "return_route IN ('caller','direct','group')",
    )
    op.create_index(
        "ix_agent_invocation_run_status", "agent_invocation", ["run_id", "status", "created_at"]
    )
    op.create_unique_constraint(
        "uq_agent_invocation_operation", "agent_invocation",
        ["caller_work_item_id", "operation_key"],
    )
    op.alter_column(
        "agent_invocation", "target_agent_version_id",
        existing_type=sa.String(36), nullable=True,
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_agent_invocation_operation", "agent_invocation", type_="unique",
    )
    for column, existing_type in (
        ("operation_key", sa.String(191)),
        ("caller_agent_id", sa.String(36)),
        ("run_id", sa.String(36)),
    ):
        op.alter_column(
            "agent_invocation", column,
            existing_type=existing_type, nullable=True,
        )
    op.alter_column(
        "agent_invocation", "target_agent_version_id",
        existing_type=sa.String(36), nullable=False,
    )
    op.drop_index("ix_agent_invocation_run_status", table_name="agent_invocation")
    op.drop_constraint("ck_agent_invocation_return_route", "agent_invocation", type_="check")
    op.drop_constraint("fk_agent_invocation_caller_agent", "agent_invocation", type_="foreignkey")
    op.drop_constraint("fk_agent_invocation_result_message", "agent_invocation", type_="foreignkey")
    op.drop_constraint("fk_agent_invocation_run", "agent_invocation", type_="foreignkey")
    op.drop_column("agent_invocation", "completed_at")
    op.drop_column("agent_invocation", "authorization_snapshot")
    op.drop_column("agent_invocation", "result_message_id")
    op.drop_column("agent_invocation", "request_hash")
    op.drop_column("agent_invocation", "operation_key")
    op.drop_column("agent_invocation", "return_route")
    op.drop_column("agent_invocation", "caller_agent_id")
    op.drop_column("agent_invocation", "run_id")
    op.drop_column("approval", "subject_snapshot")
    op.drop_constraint("ck_user_session_interaction_mode", "user_session", type_="check")
    op.drop_column("user_session", "interaction_revision")
    op.drop_column("user_session", "interaction_mode")
