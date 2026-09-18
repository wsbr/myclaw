"""Make schedules pure triggers backed by ToolInvocation definitions.

Revision ID: 0049
Revises: 0048
"""

from alembic import op
import sqlalchemy as sa


revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_agent_schedule_tool_invocation", "agent_schedule",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_agent_schedule_created_by_tool_invocation", "agent_schedule",
        type_="unique",
    )
    op.alter_column(
        "agent_schedule", "created_by_tool_invocation_id",
        new_column_name="definition_tool_invocation_id",
        existing_type=sa.String(36), existing_nullable=True,
    )
    op.create_foreign_key(
        "fk_agent_schedule_definition_tool_invocation",
        "agent_schedule", "tool_invocation",
        ["definition_tool_invocation_id"], ["id"],
    )
    op.create_unique_constraint(
        "uq_agent_schedule_definition_tool_invocation", "agent_schedule",
        ["definition_tool_invocation_id"],
    )
    for column, value_type in (
        ("agent_id", sa.String(36)),
        ("name", sa.String(200)),
        ("prompt", sa.Text()),
        ("context_mode", sa.String(24)),
        ("version_policy", sa.String(32)),
    ):
        op.alter_column(
            "agent_schedule", column, existing_type=value_type,
            nullable=True, existing_nullable=False,
        )
    op.add_column(
        "agent_schedule", sa.Column("max_occurrences", sa.BigInteger())
    )
    op.add_column(
        "agent_schedule",
        sa.Column(
            "occurrence_count", sa.BigInteger(), nullable=False,
            server_default="0",
        ),
    )
    op.execute(sa.text(
        "UPDATE agent_schedule SET max_occurrences = 1 "
        "WHERE schedule_type = 'once'"
    ))
    op.execute(sa.text(
        "UPDATE agent_schedule SET occurrence_count = ("
        "SELECT COUNT(*) FROM agent_schedule_fire "
        "WHERE agent_schedule_fire.schedule_id = agent_schedule.id "
        "AND agent_schedule_fire.trigger_kind = 'scheduled')"
    ))
    op.create_check_constraint(
        "ck_agent_schedule_occurrence_count", "agent_schedule",
        "occurrence_count >= 0",
    )
    op.create_check_constraint(
        "ck_agent_schedule_max_occurrences", "agent_schedule",
        "max_occurrences IS NULL OR max_occurrences >= 1",
    )

    op.add_column(
        "agent_schedule_fire",
        sa.Column("definition_tool_invocation_id", sa.String(36)),
    )
    op.add_column(
        "agent_schedule_fire", sa.Column("definition_hash", sa.String(64)),
    )
    op.add_column(
        "agent_schedule_fire", sa.Column("occurrence_no", sa.BigInteger()),
    )
    op.create_foreign_key(
        "fk_agent_schedule_fire_definition_tool_invocation",
        "agent_schedule_fire", "tool_invocation",
        ["definition_tool_invocation_id"], ["id"],
    )
    op.execute(sa.text(
        "UPDATE agent_schedule_fire SET "
        "definition_tool_invocation_id = ("
        "SELECT definition_tool_invocation_id FROM agent_schedule "
        "WHERE agent_schedule.id = agent_schedule_fire.schedule_id), "
        "definition_hash = prompt_hash"
    ))
    for column, value_type in (
        ("prompt_snapshot", sa.Text()),
        ("prompt_hash", sa.String(64)),
        ("context_mode_snapshot", sa.String(24)),
        ("version_policy_snapshot", sa.String(32)),
    ):
        op.alter_column(
            "agent_schedule_fire", column, existing_type=value_type,
            nullable=True, existing_nullable=False,
        )

    op.add_column(
        "runner_schedule_snapshot",
        sa.Column("definition_tool_invocation_id", sa.String(36)),
    )
    op.add_column(
        "runner_schedule_snapshot", sa.Column("definition_hash", sa.String(64)),
    )
    op.create_foreign_key(
        "fk_runner_schedule_definition_tool_invocation",
        "runner_schedule_snapshot", "tool_invocation",
        ["definition_tool_invocation_id"], ["id"],
    )
    op.execute(sa.text(
        "UPDATE runner_schedule_snapshot SET "
        "definition_tool_invocation_id = ("
        "SELECT definition_tool_invocation_id FROM agent_schedule_fire "
        "WHERE agent_schedule_fire.id = runner_schedule_snapshot.fire_id), "
        "definition_hash = prompt_hash"
    ))
    op.alter_column(
        "runner_schedule_snapshot", "prompt_hash",
        existing_type=sa.String(64), nullable=True, existing_nullable=False,
    )


def downgrade() -> None:
    op.execute(sa.text(
        "UPDATE runner_schedule_snapshot SET prompt_hash = COALESCE("
        "prompt_hash, definition_hash, "
        "'0000000000000000000000000000000000000000000000000000000000000000')"
    ))
    op.alter_column(
        "runner_schedule_snapshot", "prompt_hash",
        existing_type=sa.String(64), nullable=False, existing_nullable=True,
    )
    op.drop_constraint(
        "fk_runner_schedule_definition_tool_invocation",
        "runner_schedule_snapshot", type_="foreignkey",
    )
    op.drop_column("runner_schedule_snapshot", "definition_hash")
    op.drop_column("runner_schedule_snapshot", "definition_tool_invocation_id")

    op.execute(sa.text(
        "UPDATE agent_schedule_fire SET "
        "prompt_snapshot = COALESCE(prompt_snapshot, 'Definition archived in ToolInvocation'), "
        "prompt_hash = COALESCE(prompt_hash, definition_hash, "
        "'0000000000000000000000000000000000000000000000000000000000000000'), "
        "context_mode_snapshot = COALESCE(context_mode_snapshot, 'new_session'), "
        "version_policy_snapshot = COALESCE(version_policy_snapshot, 'latest_published')"
    ))
    for column, value_type in (
        ("prompt_snapshot", sa.Text()),
        ("prompt_hash", sa.String(64)),
        ("context_mode_snapshot", sa.String(24)),
        ("version_policy_snapshot", sa.String(32)),
    ):
        op.alter_column(
            "agent_schedule_fire", column, existing_type=value_type,
            nullable=False, existing_nullable=True,
        )
    op.drop_constraint(
        "fk_agent_schedule_fire_definition_tool_invocation",
        "agent_schedule_fire", type_="foreignkey",
    )
    op.drop_column("agent_schedule_fire", "occurrence_no")
    op.drop_column("agent_schedule_fire", "definition_hash")
    op.drop_column("agent_schedule_fire", "definition_tool_invocation_id")

    op.drop_constraint(
        "ck_agent_schedule_max_occurrences", "agent_schedule", type_="check",
    )
    op.drop_constraint(
        "ck_agent_schedule_occurrence_count", "agent_schedule", type_="check",
    )
    op.drop_column("agent_schedule", "occurrence_count")
    op.drop_column("agent_schedule", "max_occurrences")
    op.execute(sa.text(
        "UPDATE agent_schedule SET "
        "agent_id = COALESCE(agent_id, ("
        "SELECT runner_v1.agent_id FROM tool_invocation "
        "JOIN runner_v1 ON runner_v1.id = tool_invocation.runner_id "
        "WHERE tool_invocation.id = agent_schedule.definition_tool_invocation_id)), "
        "name = COALESCE(name, 'Scheduled task'), "
        "prompt = COALESCE(prompt, 'Definition archived in ToolInvocation'), "
        "context_mode = COALESCE(context_mode, 'new_session'), "
        "version_policy = COALESCE(version_policy, 'latest_published'), "
        "pinned_agent_version_id = CASE "
        "WHEN version_policy = 'latest_published' THEN NULL "
        "ELSE pinned_agent_version_id END"
    ))
    for column, value_type in (
        ("agent_id", sa.String(36)),
        ("name", sa.String(200)),
        ("prompt", sa.Text()),
        ("context_mode", sa.String(24)),
        ("version_policy", sa.String(32)),
    ):
        op.alter_column(
            "agent_schedule", column, existing_type=value_type,
            nullable=False, existing_nullable=True,
        )
    op.drop_constraint(
        "fk_agent_schedule_definition_tool_invocation", "agent_schedule",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_agent_schedule_definition_tool_invocation", "agent_schedule",
        type_="unique",
    )
    op.alter_column(
        "agent_schedule", "definition_tool_invocation_id",
        new_column_name="created_by_tool_invocation_id",
        existing_type=sa.String(36), existing_nullable=True,
    )
    op.create_foreign_key(
        "fk_agent_schedule_tool_invocation", "agent_schedule", "tool_invocation",
        ["created_by_tool_invocation_id"], ["id"],
    )
    op.create_unique_constraint(
        "uq_agent_schedule_created_by_tool_invocation", "agent_schedule",
        ["created_by_tool_invocation_id"],
    )
