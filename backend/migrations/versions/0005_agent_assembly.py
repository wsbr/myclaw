"""phase-one Agent draft and immutable publication assembly

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-02 00:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_account_id", sa.String(36), sa.ForeignKey("platform_account.id"), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False, server_default="private"),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("draft_version_id", sa.String(36)),
        sa.Column("current_published_version_id", sa.String(36)),
        sa.Column("next_version_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("visibility IN ('private','enterprise')", name="ck_agent_visibility"),
        sa.CheckConstraint("status IN ('draft','published','inactive')", name="ck_agent_status"),
    )
    op.create_index("ix_agent_owner_status", "agent", ["owner_account_id", "status"])
    op.create_index("ix_agent_visibility_status", "agent", ["visibility", "status"])
    op.create_table(
        "agent_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("version_no", sa.Integer()),
        sa.Column("publication_status", sa.String(32), nullable=False),
        sa.Column("draft_revision", sa.BigInteger()),
        sa.Column(
            "draft_agent_id",
            sa.String(36),
            sa.Computed("CASE WHEN publication_status = 'draft' THEN agent_id ELSE NULL END", persisted=True),
        ),
        sa.Column("derived_from_version_id", sa.String(36), sa.ForeignKey("agent_version.id")),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("model_id", sa.String(36), sa.ForeignKey("model.id")),
        sa.Column("sandbox_profile_id", sa.String(36), sa.ForeignKey("sandbox_profile.id")),
        sa.Column("context_policy_id", sa.String(36), sa.ForeignKey("policy_definition.id")),
        sa.Column("history_policy_id", sa.String(36), sa.ForeignKey("policy_definition.id")),
        sa.Column("memory_write_policy_id", sa.String(36), sa.ForeignKey("policy_definition.id")),
        sa.Column("memory_recall_policy_id", sa.String(36), sa.ForeignKey("policy_definition.id")),
        sa.Column("group_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_account_id", sa.String(36), sa.ForeignKey("platform_account.id"), nullable=False),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("immutable_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("agent_id", "version_no", name="uq_agent_version_number"),
        sa.UniqueConstraint("draft_agent_id", name="uq_agent_single_draft"),
        sa.CheckConstraint(
            "publication_status IN ('draft','published','deprecated','draft_test_snapshot')",
            name="ck_agent_version_publication_status",
        ),
        sa.CheckConstraint(
            "(publication_status IN ('published','deprecated') AND version_no IS NOT NULL) OR "
            "(publication_status IN ('draft','draft_test_snapshot') AND version_no IS NULL)",
            name="ck_agent_version_number_status",
        ),
    )
    op.create_index("ix_agent_version_agent_status", "agent_version", ["agent_id", "publication_status"])
    op.create_foreign_key("fk_agent_draft_version", "agent", "agent_version", ["draft_version_id"], ["id"])
    op.create_foreign_key("fk_agent_published_version", "agent", "agent_version", ["current_published_version_id"], ["id"])

    op.create_table(
        "agent_version_tool",
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("platform_tool_id", sa.String(36), sa.ForeignKey("platform_tool.id"), primary_key=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("agent_version_id", "sort_order", name="uq_agent_version_tool_order"),
    )
    op.create_table(
        "agent_version_skill",
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("skill.id"), primary_key=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("agent_version_id", "sort_order", name="uq_agent_version_skill_order"),
    )
    op.create_table(
        "agent_version_binding",
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("bound_agent_id", sa.String(36), sa.ForeignKey("agent.id"), primary_key=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("agent_version_id", "sort_order", name="uq_agent_version_binding_order"),
    )

    op.execute(
        """
        CREATE TRIGGER trg_agent_version_sealed_update
        BEFORE UPDATE ON agent_version FOR EACH ROW
        BEGIN
          IF OLD.immutable_at IS NOT NULL AND NOT (
            OLD.publication_status = 'published' AND NEW.publication_status = 'deprecated'
            AND OLD.id <=> NEW.id AND OLD.agent_id <=> NEW.agent_id
            AND OLD.version_no <=> NEW.version_no AND OLD.draft_revision <=> NEW.draft_revision
            AND OLD.derived_from_version_id <=> NEW.derived_from_version_id
            AND OLD.name <=> NEW.name AND OLD.description <=> NEW.description
            AND OLD.instructions <=> NEW.instructions AND OLD.model_id <=> NEW.model_id
            AND OLD.sandbox_profile_id <=> NEW.sandbox_profile_id
            AND OLD.context_policy_id <=> NEW.context_policy_id
            AND OLD.history_policy_id <=> NEW.history_policy_id
            AND OLD.memory_write_policy_id <=> NEW.memory_write_policy_id
            AND OLD.memory_recall_policy_id <=> NEW.memory_recall_policy_id
            AND OLD.group_enabled <=> NEW.group_enabled AND OLD.created_by_account_id <=> NEW.created_by_account_id
            AND OLD.published_at <=> NEW.published_at AND OLD.content_hash <=> NEW.content_hash
            AND OLD.immutable_at <=> NEW.immutable_at
          ) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'sealed agent version is immutable';
          END IF;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_version_sealed_delete
        BEFORE DELETE ON agent_version FOR EACH ROW
        BEGIN
          IF OLD.immutable_at IS NOT NULL THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'sealed agent version cannot be deleted';
          END IF;
        END
        """
    )
    for table in ("agent_version_tool", "agent_version_skill", "agent_version_binding"):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            reference = "NEW" if operation != "DELETE" else "OLD"
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_sealed_{operation.lower()}
                BEFORE {operation} ON {table} FOR EACH ROW
                BEGIN
                  IF EXISTS (
                    SELECT 1 FROM agent_version
                    WHERE id = {reference}.agent_version_id AND immutable_at IS NOT NULL
                  ) THEN
                    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'sealed agent assembly is immutable';
                  END IF;
                END
                """
            )


def downgrade() -> None:
    for table in ("agent_version_tool", "agent_version_skill", "agent_version_binding"):
        for operation in ("insert", "update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_sealed_{operation}")
    op.execute("DROP TRIGGER IF EXISTS trg_agent_version_sealed_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_agent_version_sealed_update")
    op.drop_table("agent_version_binding")
    op.drop_table("agent_version_skill")
    op.drop_table("agent_version_tool")
    op.drop_constraint("fk_agent_published_version", "agent", type_="foreignkey")
    op.drop_constraint("fk_agent_draft_version", "agent", type_="foreignkey")
    op.drop_table("agent_version")
    op.drop_table("agent")
