"""agent-native v2 runtime state and attempt foundation

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


JSON_EMPTY_OBJECT = sa.text("(JSON_OBJECT())")


def upgrade() -> None:
    op.add_column("user_session", sa.Column("account_id", sa.String(36)))
    op.add_column(
        "user_session",
        sa.Column("permission_mode", sa.String(32), nullable=False, server_default="standard"),
    )
    op.add_column(
        "user_session",
        sa.Column("permission_revision", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.create_foreign_key(
        "fk_user_session_account_v2", "user_session", "platform_account", ["account_id"], ["id"]
    )

    op.add_column("run", sa.Column("status", sa.String(32), nullable=False, server_default="active"))
    op.add_column("run", sa.Column("root_work_item_id", sa.String(36)))
    op.add_column(
        "run", sa.Column("permission_mode", sa.String(32), nullable=False, server_default="standard")
    )
    op.add_column(
        "run", sa.Column("hard_budget_snapshot", sa.JSON(), nullable=False, server_default=JSON_EMPTY_OBJECT)
    )
    op.add_column("run", sa.Column("completed_at", sa.DateTime()))
    op.create_check_constraint(
        "ck_run_v2_status", "run",
        "status IN ('active','waiting_user','waiting_approval','waiting_tool','waiting_agent','completed','cancelled','failed')",
    )

    op.add_column(
        "work_item", sa.Column("purpose", sa.String(64), nullable=False, server_default="root_request")
    )
    op.add_column("work_item", sa.Column("wait_reason", sa.String(64)))
    op.add_column(
        "work_item", sa.Column("resume_cursor", sa.JSON(), nullable=False, server_default=JSON_EMPTY_OBJECT)
    )

    op.add_column(
        "runner_v1", sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default="1")
    )
    op.add_column("runner_v1", sa.Column("capability_snapshot_id", sa.String(36)))
    op.add_column("runner_v1", sa.Column("started_at", sa.DateTime()))
    op.add_column("runner_v1", sa.Column("ended_at", sa.DateTime()))
    op.add_column("runner_v1", sa.Column("failure_code", sa.String(100)))

    op.create_table(
        "capability_snapshot",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), nullable=False),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id"), nullable=False),
        sa.Column("execution_principal", sa.JSON(), nullable=False),
        sa.Column("assembled_tool_keys", sa.JSON(), nullable=False),
        sa.Column("effective_tool_keys", sa.JSON(), nullable=False),
        sa.Column("denied_capabilities", sa.JSON(), nullable=False),
        sa.Column("discoverable_agents_scope", sa.JSON(), nullable=False),
        sa.Column("callable_agents_scope", sa.JSON(), nullable=False),
        sa.Column("permission_mode", sa.String(32), nullable=False),
        sa.Column("governance_versions", sa.JSON(), nullable=False),
        sa.Column("budget_snapshot", sa.JSON(), nullable=False),
        sa.Column("runtime_release_id", sa.String(64), nullable=False),
        sa.Column("harness_policy_versions", sa.JSON(), nullable=False),
        sa.Column("provider_adapter_version", sa.String(64), nullable=False),
        sa.Column("tool_schema_bundle_hash", sa.String(64), nullable=False),
        sa.Column("experiment_cohort", sa.String(100)),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("runner_id", name="uq_capability_snapshot_runner"),
    )
    op.create_index(
        "ix_capability_snapshot_work_item", "capability_snapshot", ["work_item_id", "created_at"]
    )
    op.create_foreign_key(
        "fk_runner_capability_snapshot", "runner_v1", "capability_snapshot",
        ["capability_snapshot_id"], ["id"],
    )

    op.create_table(
        "agent_turn",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), nullable=False),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id"), nullable=False),
        sa.Column("capability_snapshot_id", sa.String(36), sa.ForeignKey("capability_snapshot.id"), nullable=False),
        sa.Column("turn_no", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="started"),
        sa.Column("context_manifest", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("turn_items", sa.JSON(), nullable=False),
        sa.Column("stop_reason", sa.String(64)),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("provider_state_ref", sa.String(500)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "status IN ('started','waiting_actions','completed','failed')",
            name="ck_agent_turn_status",
        ),
        sa.UniqueConstraint("runner_id", "turn_no", name="uq_agent_turn_number"),
    )
    op.create_index("ix_agent_turn_work_item", "agent_turn", ["work_item_id", "created_at"])

    op.create_table(
        "provider_attempt",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_turn_id", sa.String(36), sa.ForeignKey("agent_turn.id"), nullable=False),
        sa.Column("model_version_id", sa.String(36), sa.ForeignKey("model_version.id"), nullable=False),
        sa.Column("attempt_no", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(191), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("provider_request_id", sa.String(191)),
        sa.Column("provider_state_ref", sa.String(500)),
        sa.Column("received_payload", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tool_call_observed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("side_effect_committed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stop_reason", sa.String(64)),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','timed_out','outcome_unknown')",
            name="ck_provider_attempt_status",
        ),
        sa.UniqueConstraint("agent_turn_id", "attempt_no", name="uq_provider_attempt_number"),
    )
    op.create_index("ix_provider_attempt_status", "provider_attempt", ["status", "started_at"])

    for name, column in (
        ("execution_epoch", sa.Column("execution_epoch", sa.BigInteger(), nullable=False, server_default="1")),
        ("capability_snapshot_id", sa.Column("capability_snapshot_id", sa.String(36))),
        ("context_manifest", sa.Column("context_manifest", sa.JSON(), nullable=False, server_default=JSON_EMPTY_OBJECT)),
        ("compaction_ref", sa.Column("compaction_ref", sa.String(500))),
        ("working_plan_revision", sa.Column("working_plan_revision", sa.BigInteger())),
        ("result_cursor", sa.Column("result_cursor", sa.JSON(), nullable=False, server_default=JSON_EMPTY_OBJECT)),
        ("pending_refs", sa.Column("pending_refs", sa.JSON(), nullable=False, server_default=JSON_EMPTY_OBJECT)),
        ("budget_snapshot", sa.Column("budget_snapshot", sa.JSON(), nullable=False, server_default=JSON_EMPTY_OBJECT)),
        ("content_hash", sa.Column("content_hash", sa.String(64))),
    ):
        del name
        op.add_column("runner_checkpoint", column)
    op.create_foreign_key(
        "fk_checkpoint_capability_snapshot", "runner_checkpoint", "capability_snapshot",
        ["capability_snapshot_id"], ["id"],
    )

    for column in (
        sa.Column("source_turn_id", sa.String(36)),
        sa.Column("provider_tool_call_id", sa.String(191)),
        sa.Column("operation_key", sa.String(191)),
        sa.Column("capability_snapshot_id", sa.String(36)),
        sa.Column("tool_registration_hash", sa.String(64)),
        sa.Column("authorization_snapshot", sa.JSON(), nullable=False, server_default=JSON_EMPTY_OBJECT),
    ):
        op.add_column("tool_invocation", column)
    op.create_foreign_key(
        "fk_tool_invocation_source_turn", "tool_invocation", "agent_turn", ["source_turn_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_tool_invocation_capability_snapshot", "tool_invocation", "capability_snapshot",
        ["capability_snapshot_id"], ["id"],
    )
    op.create_unique_constraint(
        "uq_tool_invocation_provider_call", "tool_invocation",
        ["work_item_id", "source_turn_id", "provider_tool_call_id"],
    )

    for table in ("capability_snapshot", "runner_checkpoint"):
        op.execute(sa.text(
            f"CREATE TRIGGER trg_{table}_immutable_update BEFORE UPDATE ON {table} FOR EACH ROW "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is append-only'"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_{table}_immutable_delete BEFORE DELETE ON {table} FOR EACH ROW "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is append-only'"
        ))
    for table in ("agent_turn", "provider_attempt"):
        op.execute(sa.text(f"""
            CREATE TRIGGER trg_{table}_terminal_update
            BEFORE UPDATE ON {table} FOR EACH ROW
            BEGIN
              IF OLD.status IN ('completed','failed','succeeded','timed_out','outcome_unknown') THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'terminal attempt fact is immutable';
              END IF;
            END
        """))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_{table}_immutable_delete BEFORE DELETE ON {table} FOR EACH ROW "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is append-only'"
        ))


def downgrade() -> None:
    for table in ("provider_attempt", "agent_turn"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_terminal_update")
    for table in ("runner_checkpoint", "capability_snapshot"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_update")
    op.drop_constraint("uq_tool_invocation_provider_call", "tool_invocation", type_="unique")
    op.drop_constraint("fk_tool_invocation_capability_snapshot", "tool_invocation", type_="foreignkey")
    op.drop_constraint("fk_tool_invocation_source_turn", "tool_invocation", type_="foreignkey")
    for column in (
        "authorization_snapshot", "tool_registration_hash", "capability_snapshot_id",
        "operation_key", "provider_tool_call_id", "source_turn_id",
    ):
        op.drop_column("tool_invocation", column)
    op.drop_constraint("fk_checkpoint_capability_snapshot", "runner_checkpoint", type_="foreignkey")
    for column in (
        "content_hash", "budget_snapshot", "pending_refs", "result_cursor",
        "working_plan_revision", "compaction_ref", "context_manifest",
        "capability_snapshot_id", "execution_epoch",
    ):
        op.drop_column("runner_checkpoint", column)
    op.drop_table("provider_attempt")
    op.drop_table("agent_turn")
    op.drop_constraint("fk_runner_capability_snapshot", "runner_v1", type_="foreignkey")
    op.drop_table("capability_snapshot")
    for column in ("failure_code", "ended_at", "started_at", "capability_snapshot_id", "fencing_token"):
        op.drop_column("runner_v1", column)
    for column in ("resume_cursor", "wait_reason", "purpose"):
        op.drop_column("work_item", column)
    op.drop_constraint("ck_run_v2_status", "run", type_="check")
    for column in ("completed_at", "hard_budget_snapshot", "permission_mode", "root_work_item_id", "status"):
        op.drop_column("run", column)
    op.drop_constraint("fk_user_session_account_v2", "user_session", type_="foreignkey")
    for column in ("permission_revision", "permission_mode", "account_id"):
        op.drop_column("user_session", column)
