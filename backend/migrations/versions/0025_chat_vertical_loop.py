"""phase-one Chat vertical loop state

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


JSON_EMPTY_OBJECT = sa.text("(JSON_OBJECT())")
JSON_EMPTY_ARRAY = sa.text("(JSON_ARRAY())")


def upgrade() -> None:
    op.execute("UPDATE user_session SET permission_mode='direct' WHERE permission_mode='standard'")
    op.execute("UPDATE run SET permission_mode='direct' WHERE permission_mode='standard'")
    op.alter_column(
        "user_session", "permission_mode", existing_type=sa.String(32),
        nullable=False, server_default="direct",
    )
    op.alter_column(
        "run", "permission_mode", existing_type=sa.String(32),
        nullable=False, server_default="direct",
    )

    op.drop_constraint("ck_work_item_status", "work_item", type_="check")
    op.create_check_constraint(
        "ck_work_item_status", "work_item",
        "status IN ('queued','running','waiting_user','waiting_approval','waiting_agent','waiting_tool','waiting_resource','succeeded','failed','timed_out','cancelled','abandoned','blocked')",
    )

    op.create_table(
        "working_plan",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("owner_agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "status IN ('active','superseded','closed')", name="ck_working_plan_status"
        ),
    )
    op.create_index(
        "ix_working_plan_run_owner_status", "working_plan",
        ["run_id", "owner_agent_id", "status", "updated_at"],
    )
    op.create_table(
        "plan_update",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("working_plan_id", sa.String(36), sa.ForeignKey("working_plan.id"), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False, server_default=JSON_EMPTY_ARRAY),
        sa.Column("explanation", sa.Text()),
        sa.Column("updated_by_runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), nullable=False),
        sa.Column("source_turn_id", sa.String(36), sa.ForeignKey("agent_turn.id")),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("working_plan_id", "revision", name="uq_plan_update_revision"),
    )
    op.create_index(
        "ix_plan_update_plan_created", "plan_update", ["working_plan_id", "created_at"]
    )

    op.add_column("clarification", sa.Column("source_turn_id", sa.String(36)))
    op.add_column("clarification", sa.Column("provider_tool_call_id", sa.String(191)))
    op.create_foreign_key(
        "fk_clarification_source_turn", "clarification", "agent_turn",
        ["source_turn_id"], ["id"],
    )

    op.create_table(
        "approval",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("requested_by_agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("subject_type", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("subject_version", sa.BigInteger(), nullable=False),
        sa.Column("target_account_id", sa.String(36), sa.ForeignKey("platform_account.id"), nullable=False),
        sa.Column("subject_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("presentation", sa.JSON(), nullable=False, server_default=JSON_EMPTY_OBJECT),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("decision_reason", sa.Text()),
        sa.Column("decided_by_account_id", sa.String(36), sa.ForeignKey("platform_account.id")),
        sa.Column("decided_at", sa.DateTime()),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "kind IN ('plan_implementation','tool','membership','session_grant','reporter_selection')",
            name="ck_approval_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','cancelled')",
            name="ck_approval_status",
        ),
        sa.UniqueConstraint(
            "kind", "subject_type", "subject_id", "subject_hash",
            name="uq_approval_subject",
        ),
        sa.UniqueConstraint("work_item_id", "idempotency_key", name="uq_approval_work_key"),
    )
    op.create_index(
        "ix_approval_target_status_created", "approval",
        ["target_account_id", "status", "created_at"],
    )
    op.create_table(
        "session_grant",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("target_account_id", sa.String(36), sa.ForeignKey("platform_account.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("tool_key", sa.String(191), nullable=False),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False, server_default=JSON_EMPTY_OBJECT),
        sa.Column("source_approval_id", sa.String(36), sa.ForeignKey("approval.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "status IN ('active','revoked','expired')", name="ck_session_grant_status"
        ),
    )
    op.create_index(
        "ix_session_grant_match", "session_grant",
        ["session_id", "target_account_id", "tool_key", "status", "expires_at"],
    )

    op.drop_constraint("ck_artifact_status", "artifact", type_="check")
    op.create_check_constraint(
        "ck_artifact_status", "artifact",
        "status IN ('publishing','published','unavailable','deleted')",
    )
    op.add_column("artifact", sa.Column("source_relative_path", sa.String(500)))

    op.execute(
        "CREATE TRIGGER trg_plan_update_immutable_update BEFORE UPDATE ON plan_update "
        "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'plan_update is append-only'"
    )
    op.execute(
        "CREATE TRIGGER trg_plan_update_immutable_delete BEFORE DELETE ON plan_update "
        "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'plan_update is append-only'"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_plan_update_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_plan_update_immutable_update")
    op.drop_column("artifact", "source_relative_path")
    op.drop_constraint("ck_artifact_status", "artifact", type_="check")
    op.create_check_constraint(
        "ck_artifact_status", "artifact",
        "status IN ('published','unavailable','deleted')",
    )
    op.drop_table("session_grant")
    op.drop_table("approval")
    op.drop_constraint("fk_clarification_source_turn", "clarification", type_="foreignkey")
    op.drop_column("clarification", "provider_tool_call_id")
    op.drop_column("clarification", "source_turn_id")
    op.drop_table("plan_update")
    op.drop_table("working_plan")
    op.drop_constraint("ck_work_item_status", "work_item", type_="check")
    op.create_check_constraint(
        "ck_work_item_status", "work_item",
        "status IN ('queued','running','waiting_user','waiting_agent','waiting_tool','waiting_resource','succeeded','failed','timed_out','cancelled','abandoned','blocked')",
    )
    op.alter_column(
        "run", "permission_mode", existing_type=sa.String(32),
        nullable=False, server_default="standard",
    )
    op.alter_column(
        "user_session", "permission_mode", existing_type=sa.String(32),
        nullable=False, server_default="standard",
    )
