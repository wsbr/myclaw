"""phase-one Agent Network and Group membership vertical loop

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("group_session", sa.Column("initiator_agent_id", sa.String(36)))
    op.add_column(
        "group_session",
        sa.Column(
            "membership_policy", sa.String(32), nullable=False,
            server_default="fixed_for_run",
        ),
    )
    op.execute("UPDATE group_session SET initiator_agent_id=entry_agent_id")
    op.create_foreign_key(
        "fk_group_session_initiator", "group_session", "agent",
        ["initiator_agent_id"], ["id"],
    )

    op.alter_column(
        "group_run", "owner_node_id", existing_type=sa.String(36), nullable=True,
    )
    op.add_column("group_run", sa.Column("reporter_agent_id", sa.String(36)))
    op.add_column(
        "group_run", sa.Column("summary_epoch", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column(
        "group_run", sa.Column("delivered_summary_epoch", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column(
        "group_run", sa.Column("shared_context", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())"))
    )
    op.add_column(
        "group_run", sa.Column("budget_snapshot", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())"))
    )
    op.add_column(
        "group_run", sa.Column("policy_snapshot", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())"))
    )
    op.execute(
        "UPDATE group_run gr JOIN group_agent_node n ON n.id=gr.owner_node_id "
        "SET gr.reporter_agent_id=n.agent_id"
    )
    op.create_foreign_key(
        "fk_group_run_reporter", "group_run", "agent", ["reporter_agent_id"], ["id"]
    )
    # V1 froze the whole GroupRun row because it only contained snapshot
    # identity. Agent-native GroupRun adds lifecycle control fields on that
    # aggregate. Keep snapshot facts immutable while allowing Reporter
    # replacement and monotonic summary/runtime bookkeeping.
    op.execute("DROP TRIGGER IF EXISTS trg_group_run_immutable_update")
    op.execute(sa.text("""
        CREATE TRIGGER trg_group_run_immutable_update
        BEFORE UPDATE ON group_run FOR EACH ROW
        BEGIN
          IF NOT (
            NEW.id <=> OLD.id
            AND NEW.run_id <=> OLD.run_id
            AND NEW.group_session_id <=> OLD.group_session_id
            AND NEW.owner_node_id <=> OLD.owner_node_id
            AND NEW.goal <=> OLD.goal
            AND NEW.shared_context <=> OLD.shared_context
            AND NEW.budget_snapshot <=> OLD.budget_snapshot
            AND NEW.policy_snapshot <=> OLD.policy_snapshot
            AND NEW.created_at <=> OLD.created_at
          ) THEN
            SIGNAL SQLSTATE '45000'
              SET MESSAGE_TEXT = 'GroupRun snapshot facts are immutable';
          END IF;
        END
    """))

    op.create_table(
        "group_membership",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("group_session_id", sa.String(36), sa.ForeignKey("group_session.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("membership_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="joined"),
        sa.Column("invited_by_kind", sa.String(20), nullable=False, server_default="user"),
        sa.Column("invited_by_account_id", sa.String(36), sa.ForeignKey("platform_account.id")),
        sa.Column("invited_by_agent_id", sa.String(36), sa.ForeignKey("agent.id")),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("context_scope", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())")),
        sa.Column("authorization_source", sa.String(191), nullable=False, server_default="user_selected"),
        sa.Column("joined_at", sa.DateTime()),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('invited','joined','declined','cancelled','left','removed')",
            name="ck_group_membership_status",
        ),
        sa.UniqueConstraint(
            "group_session_id", "agent_id", "membership_no",
            name="uq_group_membership_agent_event",
        ),
    )
    op.create_index(
        "ix_group_membership_group_status", "group_membership",
        ["group_session_id", "status", "created_at"],
    )

    op.create_table(
        "group_run_member",
        sa.Column("group_run_id", sa.String(36), sa.ForeignKey("group_run.id"), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id"), primary_key=True),
        sa.Column("membership_id", sa.String(36), sa.ForeignKey("group_membership.id"), nullable=False),
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id")),
        sa.Column("participation_status", sa.String(20), nullable=False, server_default="available"),
        sa.Column("unavailable_reason", sa.String(191)),
        sa.Column("membership_policy", sa.String(32), nullable=False, server_default="fixed_for_run"),
        sa.Column("context_scope", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())")),
        sa.Column("capability_boundary", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())")),
        sa.Column("budget_snapshot", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())")),
    )

    op.create_table(
        "group_summary_request",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("group_run_id", sa.String(36), sa.ForeignKey("group_run.id"), nullable=False),
        sa.Column("summary_epoch", sa.BigInteger(), nullable=False),
        sa.Column("reporter_work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("trigger_event_id", sa.String(36), sa.ForeignKey("execution_event.id")),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("final_message_id", sa.String(36), sa.ForeignKey("message.id")),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','running','delivered','superseded','failed')",
            name="ck_group_summary_status",
        ),
        sa.UniqueConstraint("group_run_id", "summary_epoch", name="uq_group_summary_epoch"),
    )

    op.alter_column(
        "group_artifact", "published_by_node_id",
        existing_type=sa.String(36), nullable=True,
    )
    op.add_column(
        "group_artifact",
        sa.Column("published_by_membership_id", sa.String(36)),
    )
    op.create_foreign_key(
        "fk_group_artifact_membership", "group_artifact", "group_membership",
        ["published_by_membership_id"], ["id"],
    )
    op.create_check_constraint(
        "ck_group_artifact_publisher_scope", "group_artifact",
        "(published_by_node_id IS NOT NULL) <> (published_by_membership_id IS NOT NULL)",
    )

    op.create_table(
        "shared_task",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("group_session_id", sa.String(36), sa.ForeignKey("group_session.id"), nullable=False),
        sa.Column("group_run_id", sa.String(36), sa.ForeignKey("group_run.id")),
        sa.Column("source_operation_key", sa.String(191)),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("assignee_agent_id", sa.String(36), sa.ForeignKey("agent.id")),
        sa.Column("depends_on_task_ids", sa.JSON(), nullable=False, server_default=sa.text("(JSON_ARRAY())")),
        sa.Column("result_refs", sa.JSON(), nullable=False, server_default=sa.text("(JSON_ARRAY())")),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','in_progress','completed','blocked','cancelled')",
            name="ck_shared_task_status",
        ),
        sa.UniqueConstraint(
            "group_run_id", "source_operation_key",
            name="uq_shared_task_run_operation",
        ),
    )
    op.create_index(
        "ix_shared_task_group_status", "shared_task", ["group_session_id", "status"]
    )

    op.add_column(
        "message", sa.Column("visibility", sa.String(20), nullable=False, server_default="user")
    )
    op.add_column("message", sa.Column("recipient_agent_id", sa.String(36)))
    op.add_column("message", sa.Column("sequence_no", sa.BigInteger()))
    op.add_column("message", sa.Column("operation_key", sa.String(191)))
    op.create_foreign_key(
        "fk_message_recipient_agent", "message", "agent", ["recipient_agent_id"], ["id"]
    )
    op.create_index("ix_message_group_sequence", "message", ["group_session_id", "sequence_no"])
    op.create_unique_constraint(
        "uq_group_message_operation_key", "message", ["group_run_id", "operation_key"]
    )

    op.add_column("agent_invocation", sa.Column("group_session_id", sa.String(36)))
    op.add_column("agent_invocation", sa.Column("group_run_id", sa.String(36)))
    op.create_foreign_key(
        "fk_agent_invocation_group_session", "agent_invocation", "group_session",
        ["group_session_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_agent_invocation_group_run", "agent_invocation", "group_run",
        ["group_run_id"], ["id"],
    )
    op.create_index(
        "ix_agent_invocation_group_run", "agent_invocation",
        ["group_run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_invocation_group_run", table_name="agent_invocation")
    op.drop_constraint("fk_agent_invocation_group_run", "agent_invocation", type_="foreignkey")
    op.drop_constraint("fk_agent_invocation_group_session", "agent_invocation", type_="foreignkey")
    op.drop_column("agent_invocation", "group_run_id")
    op.drop_column("agent_invocation", "group_session_id")
    op.drop_constraint("uq_group_message_operation_key", "message", type_="unique")
    op.drop_index("ix_message_group_sequence", table_name="message")
    op.drop_constraint("fk_message_recipient_agent", "message", type_="foreignkey")
    op.drop_column("message", "sequence_no")
    op.drop_column("message", "operation_key")
    op.drop_column("message", "recipient_agent_id")
    op.drop_column("message", "visibility")
    op.drop_table("shared_task")
    op.drop_constraint("ck_group_artifact_publisher_scope", "group_artifact", type_="check")
    op.drop_constraint("fk_group_artifact_membership", "group_artifact", type_="foreignkey")
    op.execute("DELETE FROM group_artifact WHERE published_by_node_id IS NULL")
    op.drop_column("group_artifact", "published_by_membership_id")
    op.alter_column(
        "group_artifact", "published_by_node_id",
        existing_type=sa.String(36), nullable=False,
    )
    op.drop_table("group_summary_request")
    op.drop_table("group_run_member")
    op.drop_table("group_membership")
    op.drop_constraint("fk_group_run_reporter", "group_run", type_="foreignkey")
    op.execute("DROP TRIGGER IF EXISTS trg_group_run_immutable_update")
    for column in (
        "policy_snapshot", "budget_snapshot", "shared_context",
        "delivered_summary_epoch", "summary_epoch", "reporter_agent_id",
    ):
        op.drop_column("group_run", column)
    op.alter_column("group_run", "owner_node_id", existing_type=sa.String(36), nullable=False)
    op.execute(sa.text("""
        CREATE TRIGGER trg_group_run_immutable_update
        BEFORE UPDATE ON group_run FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'group_run is immutable'
    """))
    op.drop_constraint("fk_group_session_initiator", "group_session", type_="foreignkey")
    op.drop_column("group_session", "membership_policy")
    op.drop_column("group_session", "initiator_agent_id")
