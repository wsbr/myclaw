"""Append-only usage and cost ledger.

Revision ID: 0036
Revises: 0035
"""

from alembic import op
import sqlalchemy as sa


revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_price_snapshot",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(191)),
        sa.Column("subject_key", sa.String(191), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("unit", sa.String(64), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("price_per_unit", sa.Numeric(30, 12), nullable=False),
        sa.Column("price_status", sa.String(20), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("effective_to", sa.DateTime()),
        sa.Column("source_label", sa.String(500), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_by_account_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "subject_type IN ('model','tool','sandbox','storage','egress')",
            name="ck_usage_price_subject_type",
        ),
        sa.CheckConstraint(
            "price_status IN ('estimated','contracted')",
            name="ck_usage_price_status",
        ),
        sa.CheckConstraint("price_per_unit >= 0", name="ck_usage_price_nonnegative"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_usage_price_window",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_account_id"], ["platform_account.id"],
        ),
        sa.UniqueConstraint("content_hash", name="uq_usage_price_content_hash"),
    )
    op.create_index(
        "ix_usage_price_lookup", "usage_price_snapshot",
        ["subject_type", "provider", "subject_key", "metric", "effective_from"],
    )

    op.create_table(
        "usage_ledger_entry",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("group_run_id", sa.String(36)),
        sa.Column("work_item_id", sa.String(36)),
        sa.Column("runner_id", sa.String(36)),
        sa.Column("agent_version_id", sa.String(36)),
        sa.Column("agent_turn_id", sa.String(36)),
        sa.Column("provider_attempt_id", sa.String(36)),
        sa.Column("model_version_id", sa.String(36)),
        sa.Column("tool_invocation_id", sa.String(36)),
        sa.Column("tool_attempt_id", sa.String(36)),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("source_operation_key", sa.String(191), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("unit", sa.String(64), nullable=False),
        sa.Column("quantity", sa.Numeric(30, 9), nullable=False),
        sa.Column("measurement_status", sa.String(20), nullable=False),
        sa.Column("price_snapshot_id", sa.String(36)),
        sa.Column("currency", sa.String(3)),
        sa.Column("unit_price_snapshot", sa.Numeric(30, 12)),
        sa.Column("cost_amount", sa.Numeric(30, 12)),
        sa.Column("cost_status", sa.String(20), nullable=False),
        sa.Column("adjusts_entry_id", sa.String(36)),
        sa.Column("safe_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('provider_attempt','tool_attempt','sandbox_attempt','storage_observation','egress_observation','adjustment')",
            name="ck_usage_entry_source_type",
        ),
        sa.CheckConstraint(
            "measurement_status IN ('measured','estimated','unknown')",
            name="ck_usage_entry_measurement_status",
        ),
        sa.CheckConstraint(
            "cost_status IN ('estimated','contracted','unknown')",
            name="ck_usage_entry_cost_status",
        ),
        sa.CheckConstraint(
            "source_type = 'adjustment' OR quantity >= 0",
            name="ck_usage_entry_quantity_nonnegative",
        ),
        sa.CheckConstraint(
            "(cost_status = 'unknown' AND price_snapshot_id IS NULL AND currency IS NULL AND unit_price_snapshot IS NULL AND cost_amount IS NULL) OR "
            "(cost_status IN ('estimated','contracted') AND price_snapshot_id IS NOT NULL AND currency IS NOT NULL AND unit_price_snapshot IS NOT NULL AND cost_amount IS NOT NULL)",
            name="ck_usage_entry_cost_binding",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["platform_account.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["user_session.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"]),
        sa.ForeignKeyConstraint(["group_run_id"], ["group_run.id"]),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_item.id"]),
        sa.ForeignKeyConstraint(["runner_id"], ["runner_v1.id"]),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_version.id"]),
        sa.ForeignKeyConstraint(["agent_turn_id"], ["agent_turn.id"]),
        sa.ForeignKeyConstraint(["provider_attempt_id"], ["provider_attempt.id"]),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_version.id"]),
        sa.ForeignKeyConstraint(["tool_invocation_id"], ["tool_invocation.id"]),
        sa.ForeignKeyConstraint(["tool_attempt_id"], ["tool_attempt.id"]),
        sa.ForeignKeyConstraint(["price_snapshot_id"], ["usage_price_snapshot.id"]),
        sa.ForeignKeyConstraint(["adjusts_entry_id"], ["usage_ledger_entry.id"]),
        sa.UniqueConstraint(
            "source_operation_key", "metric", name="uq_usage_entry_source_metric",
        ),
    )
    for name, columns in (
        ("ix_usage_entry_run_created", ["run_id", "created_at", "id"]),
        ("ix_usage_entry_account_created", ["account_id", "created_at", "id"]),
        ("ix_usage_entry_agent_created", ["agent_version_id", "created_at", "id"]),
        ("ix_usage_entry_model_created", ["model_version_id", "created_at", "id"]),
    ):
        op.create_index(name, "usage_ledger_entry", columns)
    for table in ("usage_price_snapshot", "usage_ledger_entry"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable_update "
            f"BEFORE UPDATE ON {table} FOR EACH ROW "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'usage ledger fact is append-only'"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable_delete "
            f"BEFORE DELETE ON {table} FOR EACH ROW "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'usage ledger fact is append-only'"
        )


def downgrade() -> None:
    for table in ("usage_ledger_entry", "usage_price_snapshot"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_update")
    for name in (
        "ix_usage_entry_model_created", "ix_usage_entry_agent_created",
        "ix_usage_entry_account_created", "ix_usage_entry_run_created",
    ):
        op.drop_index(name, table_name="usage_ledger_entry")
    op.drop_table("usage_ledger_entry")
    op.drop_index("ix_usage_price_lookup", table_name="usage_price_snapshot")
    op.drop_table("usage_price_snapshot")
