"""Versioned Agent evaluations and release gates.

Revision ID: 0037
Revises: 0036
"""

from alembic import op
import sqlalchemy as sa


revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_eval_suite",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("current_version_id", sa.String(36)),
        sa.Column("next_version_no", sa.Integer(), nullable=False),
        sa.Column("created_by_account_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("status IN ('active','inactive')", name="ck_agent_eval_suite_status"),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
        sa.ForeignKeyConstraint(["created_by_account_id"], ["platform_account.id"]),
    )
    op.create_index(
        "ix_agent_eval_suite_agent_status", "agent_eval_suite", ["agent_id", "status"],
    )
    op.create_table(
        "agent_eval_suite_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("suite_id", sa.String(36), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("gate_policy", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_by_account_id", sa.String(36), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version_no > 0", name="ck_agent_eval_suite_version_positive"),
        sa.ForeignKeyConstraint(["suite_id"], ["agent_eval_suite.id"]),
        sa.ForeignKeyConstraint(["created_by_account_id"], ["platform_account.id"]),
        sa.UniqueConstraint("suite_id", "version_no", name="uq_agent_eval_suite_version_no"),
    )
    op.create_index(
        "ix_agent_eval_suite_version_suite", "agent_eval_suite_version", ["suite_id", "version_no"],
    )
    op.create_foreign_key(
        "fk_agent_eval_suite_current_version", "agent_eval_suite",
        "agent_eval_suite_version", ["current_version_id"], ["id"],
    )
    op.create_table(
        "agent_eval_case",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("suite_version_id", sa.String(36), nullable=False),
        sa.Column("case_key", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("assertions", sa.JSON(), nullable=False),
        sa.Column("weight", sa.Numeric(12, 4), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("weight > 0", name="ck_agent_eval_case_weight_positive"),
        sa.ForeignKeyConstraint(["suite_version_id"], ["agent_eval_suite_version.id"]),
        sa.UniqueConstraint("suite_version_id", "case_key", name="uq_agent_eval_case_key"),
    )
    op.create_index(
        "ix_agent_eval_case_suite_order", "agent_eval_case", ["suite_version_id", "sort_order"],
    )
    op.create_table(
        "agent_eval_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("suite_version_id", sa.String(36), nullable=False),
        sa.Column("candidate_agent_version_id", sa.String(36), nullable=False),
        sa.Column("candidate_content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("passed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("score", sa.Numeric(12, 9)),
        sa.Column("gate_policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("created_by_account_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','running','passed','failed','error')",
            name="ck_agent_eval_run_status",
        ),
        sa.CheckConstraint(
            "(status IN ('passed','failed','error') AND ended_at IS NOT NULL) OR "
            "(status IN ('queued','running') AND ended_at IS NULL)",
            name="ck_agent_eval_run_terminal_time",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
        sa.ForeignKeyConstraint(["suite_version_id"], ["agent_eval_suite_version.id"]),
        sa.ForeignKeyConstraint(["candidate_agent_version_id"], ["agent_version.id"]),
        sa.ForeignKeyConstraint(["created_by_account_id"], ["platform_account.id"]),
    )
    op.create_index(
        "ix_agent_eval_run_gate_lookup", "agent_eval_run",
        ["agent_id", "suite_version_id", "candidate_content_hash", "status", "created_at"],
    )
    op.create_table(
        "agent_eval_case_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("eval_run_id", sa.String(36), nullable=False),
        sa.Column("eval_case_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("score", sa.Numeric(12, 9)),
        sa.Column("response_message_id", sa.String(36)),
        sa.Column("assertion_results", sa.JSON(), nullable=False),
        sa.Column("failure_code", sa.String(100)),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','running','passed','failed','error')",
            name="ck_agent_eval_case_run_status",
        ),
        sa.ForeignKeyConstraint(["eval_run_id"], ["agent_eval_run.id"]),
        sa.ForeignKeyConstraint(["eval_case_id"], ["agent_eval_case.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["user_session.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"]),
        sa.ForeignKeyConstraint(["response_message_id"], ["message.id"]),
        sa.UniqueConstraint("eval_run_id", "eval_case_id", name="uq_agent_eval_case_run"),
        sa.UniqueConstraint("run_id", name="uq_agent_eval_case_runtime_run"),
    )
    op.create_index(
        "ix_agent_eval_case_run_status", "agent_eval_case_run", ["status", "updated_at", "id"],
    )

    for table in ("agent_eval_suite_version", "agent_eval_case"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable_update "
            f"BEFORE UPDATE ON {table} FOR EACH ROW "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'agent eval definition is immutable'"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable_delete "
            f"BEFORE DELETE ON {table} FOR EACH ROW "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'agent eval definition is immutable'"
        )
    op.execute(
        "CREATE TRIGGER trg_agent_eval_run_binding_immutable "
        "BEFORE UPDATE ON agent_eval_run FOR EACH ROW BEGIN "
        "IF NOT (OLD.agent_id <=> NEW.agent_id) OR "
        "NOT (OLD.suite_version_id <=> NEW.suite_version_id) OR "
        "NOT (OLD.candidate_agent_version_id <=> NEW.candidate_agent_version_id) OR "
        "NOT (OLD.candidate_content_hash <=> NEW.candidate_content_hash) OR "
        "NOT (OLD.gate_policy_snapshot <=> NEW.gate_policy_snapshot) THEN "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'agent eval run binding is immutable'; "
        "END IF; END"
    )
    op.execute(
        "CREATE TRIGGER trg_agent_eval_case_run_binding_immutable "
        "BEFORE UPDATE ON agent_eval_case_run FOR EACH ROW BEGIN "
        "IF NOT (OLD.eval_run_id <=> NEW.eval_run_id) OR "
        "NOT (OLD.eval_case_id <=> NEW.eval_case_id) OR "
        "NOT (OLD.session_id <=> NEW.session_id) OR NOT (OLD.run_id <=> NEW.run_id) THEN "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'agent eval case run binding is immutable'; "
        "END IF; END"
    )


def downgrade() -> None:
    for trigger in (
        "trg_agent_eval_case_run_binding_immutable",
        "trg_agent_eval_run_binding_immutable",
        "trg_agent_eval_case_immutable_delete",
        "trg_agent_eval_case_immutable_update",
        "trg_agent_eval_suite_version_immutable_delete",
        "trg_agent_eval_suite_version_immutable_update",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_index("ix_agent_eval_case_run_status", table_name="agent_eval_case_run")
    op.drop_table("agent_eval_case_run")
    op.drop_index("ix_agent_eval_run_gate_lookup", table_name="agent_eval_run")
    op.drop_table("agent_eval_run")
    op.drop_index("ix_agent_eval_case_suite_order", table_name="agent_eval_case")
    op.drop_table("agent_eval_case")
    op.drop_constraint(
        "fk_agent_eval_suite_current_version", "agent_eval_suite", type_="foreignkey",
    )
    op.drop_index("ix_agent_eval_suite_version_suite", table_name="agent_eval_suite_version")
    op.drop_table("agent_eval_suite_version")
    op.drop_index("ix_agent_eval_suite_agent_status", table_name="agent_eval_suite")
    op.drop_table("agent_eval_suite")
