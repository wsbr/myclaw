"""runtime reliability convergence and deterministic verification

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


JSON_EMPTY_ARRAY = sa.text("(JSON_ARRAY())")
JSON_EMPTY_OBJECT = sa.text("(JSON_OBJECT())")


def upgrade() -> None:
    op.add_column("run", sa.Column("terminal_reason", sa.String(100)))
    op.add_column("run", sa.Column("deadline_at", sa.DateTime()))
    op.add_column(
        "group_run_member",
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
    )
    for table in ("clarification", "approval", "agent_invocation"):
        op.add_column(table, sa.Column("deadline_at", sa.DateTime()))

    op.add_column(
        "tool_invocation",
        sa.Column("execution_epoch", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.add_column("tool_invocation", sa.Column("deadline_at", sa.DateTime()))
    op.add_column("tool_invocation", sa.Column("active_attempt_id", sa.String(36)))

    for column in (
        sa.Column("execution_epoch", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("heartbeat_at", sa.DateTime()),
        sa.Column("request_hash", sa.String(64)),
        sa.Column("side_effect_started", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("outcome_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("ended_at", sa.DateTime()),
    ):
        op.add_column("tool_attempt", column)
    op.create_foreign_key(
        "fk_tool_invocation_active_attempt", "tool_invocation", "tool_attempt",
        ["active_attempt_id"], ["id"],
    )
    op.create_index(
        "ix_tool_attempt_lease", "tool_attempt", ["status", "lease_expires_at"]
    )

    op.create_table(
        "verification_result",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("group_run_id", sa.String(36), sa.ForeignKey("group_run.id")),
        sa.Column("rule_key", sa.String(191), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column("verification_epoch", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("fact_fingerprint", sa.String(64), nullable=False),
        sa.Column("observed_facts", sa.JSON(), nullable=False, server_default=JSON_EMPTY_ARRAY),
        sa.Column("missing_facts", sa.JSON(), nullable=False, server_default=JSON_EMPTY_ARRAY),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "status IN ('passed','failed','insufficient_evidence')",
            name="ck_verification_result_status",
        ),
        sa.UniqueConstraint(
            "run_id", "rule_key", "rule_version", "verification_epoch",
            name="uq_verification_result_epoch",
        ),
        sa.UniqueConstraint(
            "run_id", "rule_key", "rule_version", "fact_fingerprint",
            name="uq_verification_result_facts",
        ),
    )
    op.create_index(
        "ix_verification_result_run_rule", "verification_result",
        ["run_id", "rule_key", "created_at"],
    )
    op.create_table(
        "verification_evidence_ref",
        sa.Column(
            "verification_result_id", sa.String(36),
            sa.ForeignKey("verification_result.id"), primary_key=True,
        ),
        sa.Column("sort_order", sa.Integer(), primary_key=True),
        sa.Column("evidence_type", sa.String(32), nullable=False),
        sa.Column("tool_attempt_id", sa.String(36), sa.ForeignKey("tool_attempt.id")),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifact.id")),
        sa.Column("external_fact_ref", sa.String(2048)),
        sa.Column("workspace_generation", sa.BigInteger()),
        sa.Column("checksum", sa.String(64)),
        sa.Column("evidence_key", sa.String(191), nullable=False),
        sa.CheckConstraint(
            "evidence_type IN ('tool_attempt','artifact','workspace_checksum','business_fact')",
            name="ck_verification_evidence_type",
        ),
        sa.CheckConstraint(
            "(evidence_type = 'tool_attempt' AND tool_attempt_id IS NOT NULL "
            "AND artifact_id IS NULL AND external_fact_ref IS NULL "
            "AND workspace_generation IS NULL) OR "
            "(evidence_type = 'artifact' AND artifact_id IS NOT NULL "
            "AND tool_attempt_id IS NULL AND external_fact_ref IS NULL "
            "AND workspace_generation IS NULL AND checksum IS NOT NULL) OR "
            "(evidence_type = 'workspace_checksum' AND tool_attempt_id IS NULL "
            "AND artifact_id IS NULL AND external_fact_ref IS NOT NULL "
            "AND workspace_generation IS NOT NULL AND checksum IS NOT NULL) OR "
            "(evidence_type = 'business_fact' AND tool_attempt_id IS NULL "
            "AND artifact_id IS NULL AND external_fact_ref IS NOT NULL "
            "AND workspace_generation IS NULL AND checksum IS NOT NULL)",
            name="ck_verification_evidence_ref_shape",
        ),
        sa.UniqueConstraint(
            "verification_result_id", "evidence_type", "evidence_key",
            name="uq_verification_evidence_key",
        ),
    )
    op.create_table(
        "run_progress_mark",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), nullable=False),
        sa.Column("agent_turn_id", sa.String(36), sa.ForeignKey("agent_turn.id"), nullable=False),
        sa.Column("made_progress", sa.Boolean(), nullable=False),
        sa.Column("progress_fingerprint", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column("consecutive_no_progress", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("agent_turn_id", name="uq_run_progress_agent_turn"),
    )
    op.create_index(
        "ix_run_progress_run_created", "run_progress_mark", ["run_id", "created_at"]
    )
    op.create_table(
        "late_result_observation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_item.id")),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("original_status", sa.String(32), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("safe_result", sa.JSON(), nullable=False, server_default=JSON_EMPTY_OBJECT),
        sa.Column("observed_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "source_type IN ('tool_attempt','provider_attempt','agent_invocation')",
            name="ck_late_result_source_type",
        ),
        sa.UniqueConstraint(
            "source_type", "source_id", "result_hash",
            name="uq_late_result_source_hash",
        ),
    )
    op.create_index(
        "ix_late_result_run_observed", "late_result_observation",
        ["run_id", "observed_at"],
    )

    for table in (
        "verification_result", "verification_evidence_ref",
        "run_progress_mark", "late_result_observation",
    ):
        op.execute(sa.text(
            f"CREATE TRIGGER trg_{table}_immutable_update BEFORE UPDATE ON {table} FOR EACH ROW "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is append-only'"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_{table}_immutable_delete BEFORE DELETE ON {table} FOR EACH ROW "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is append-only'"
        ))

    op.execute(sa.text("""
        CREATE TRIGGER trg_tool_attempt_terminal_update
        BEFORE UPDATE ON tool_attempt FOR EACH ROW
        BEGIN
          IF OLD.status IN ('succeeded','failed','timed_out','cancelled','outcome_unknown') THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'terminal ToolAttempt is immutable';
          END IF;
        END
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_tool_invocation_terminal_update
        BEFORE UPDATE ON tool_invocation FOR EACH ROW
        BEGIN
          IF OLD.status IN ('succeeded','failed','rejected','cancelled','outcome_unknown') THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'terminal ToolInvocation is immutable';
          END IF;
        END
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_run_terminal_update
        BEFORE UPDATE ON run FOR EACH ROW
        BEGIN
          IF OLD.status IN ('completed','cancelled','failed') THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'terminal Run is immutable';
          END IF;
        END
    """))


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_run_terminal_update")
    op.execute("DROP TRIGGER IF EXISTS trg_tool_invocation_terminal_update")
    op.execute("DROP TRIGGER IF EXISTS trg_tool_attempt_terminal_update")
    for table in (
        "late_result_observation", "run_progress_mark",
        "verification_evidence_ref", "verification_result",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_update")
    op.drop_index("ix_late_result_run_observed", table_name="late_result_observation")
    op.drop_table("late_result_observation")
    op.drop_index("ix_run_progress_run_created", table_name="run_progress_mark")
    op.drop_table("run_progress_mark")
    op.drop_table("verification_evidence_ref")
    op.drop_index("ix_verification_result_run_rule", table_name="verification_result")
    op.drop_table("verification_result")
    op.drop_index("ix_tool_attempt_lease", table_name="tool_attempt")
    op.drop_constraint(
        "fk_tool_invocation_active_attempt", "tool_invocation", type_="foreignkey"
    )
    for column in (
        "ended_at", "started_at", "outcome_confirmed", "side_effect_started",
        "request_hash", "heartbeat_at", "lease_expires_at", "fencing_token",
        "execution_epoch",
    ):
        op.drop_column("tool_attempt", column)
    for column in ("active_attempt_id", "deadline_at", "execution_epoch"):
        op.drop_column("tool_invocation", column)
    for table in ("agent_invocation", "approval", "clarification"):
        op.drop_column(table, "deadline_at")
    op.drop_column("group_run_member", "version")
    op.drop_column("run", "deadline_at")
    op.drop_column("run", "terminal_reason")
