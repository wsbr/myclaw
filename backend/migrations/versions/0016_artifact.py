"""lightweight immutable Artifact registration

Revision ID: 0016
Revises: 0015
"""

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    ]


def immutable_relation(table: str):
    op.execute(sa.text(f"""
        CREATE TRIGGER trg_{table}_immutable_update BEFORE UPDATE ON {table} FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is immutable'
    """))
    op.execute(sa.text(f"""
        CREATE TRIGGER trg_{table}_immutable_delete BEFORE DELETE ON {table} FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is immutable'
    """))


def upgrade():
    op.alter_column("workspace_lock_request", "tool_invocation_id", existing_type=sa.String(36), nullable=True)
    op.create_table(
        "artifact",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspace.id"), nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("created_by_agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_item.id")),
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner_v1.id")),
        sa.Column("tool_invocation_id", sa.String(36), sa.ForeignKey("tool_invocation.id")),
        sa.Column("parent_artifact_id", sa.String(36), sa.ForeignKey("artifact.id")),
        sa.Column("status", sa.String(20), nullable=False, server_default="published"),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *timestamps(),
        sa.CheckConstraint("status IN ('published','unavailable','deleted')", name="ck_artifact_status"),
        sa.UniqueConstraint("session_id", "relative_path", "version_no", name="uq_artifact_path_version"),
        sa.UniqueConstraint("parent_artifact_id", "version_no", name="uq_artifact_parent_version"),
        sa.UniqueConstraint("tool_invocation_id", name="uq_artifact_tool_invocation"),
    )
    op.create_index("ix_artifact_session_status_created", "artifact", ["session_id", "status", "created_at"])
    op.create_table(
        "message_artifact",
        sa.Column("message_id", sa.String(36), sa.ForeignKey("message.id"), primary_key=True),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifact.id"), primary_key=True),
        sa.Column("visibility", sa.String(20), nullable=False),
        sa.CheckConstraint("visibility IN ('user','group','direct')", name="ck_message_artifact_visibility"),
    )
    op.create_table(
        "group_artifact",
        sa.Column("group_session_id", sa.String(36), sa.ForeignKey("group_session.id"), primary_key=True),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifact.id"), primary_key=True),
        sa.Column("published_by_node_id", sa.String(36), sa.ForeignKey("group_agent_node.id"), nullable=False),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("message.id")),
    )
    op.execute(sa.text("""
        CREATE TRIGGER trg_artifact_content_immutable_update
        BEFORE UPDATE ON artifact FOR EACH ROW
        BEGIN
          IF NOT (
            NEW.id <=> OLD.id AND NEW.user_id <=> OLD.user_id AND NEW.session_id <=> OLD.session_id
            AND NEW.workspace_id <=> OLD.workspace_id AND NEW.relative_path <=> OLD.relative_path
            AND NEW.name <=> OLD.name AND NEW.mime_type <=> OLD.mime_type
            AND NEW.size_bytes <=> OLD.size_bytes AND NEW.checksum <=> OLD.checksum
            AND NEW.created_by_agent_id <=> OLD.created_by_agent_id
            AND NEW.work_item_id <=> OLD.work_item_id AND NEW.runner_id <=> OLD.runner_id
            AND NEW.tool_invocation_id <=> OLD.tool_invocation_id
            AND NEW.parent_artifact_id <=> OLD.parent_artifact_id
            AND NEW.version_no <=> OLD.version_no AND NEW.created_at <=> OLD.created_at
          ) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Artifact content metadata is immutable';
          END IF;
          IF OLD.status = 'deleted' AND NEW.status <> 'deleted' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Deleted Artifact is terminal';
          END IF;
          IF OLD.status = 'unavailable' AND NEW.status = 'published' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Unavailable Artifact cannot be republished';
          END IF;
        END
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_artifact_immutable_delete BEFORE DELETE ON artifact FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Artifact audit row is immutable'
    """))
    immutable_relation("message_artifact")
    immutable_relation("group_artifact")


def downgrade():
    for trigger in (
        "trg_artifact_content_immutable_update", "trg_artifact_immutable_delete",
        "trg_message_artifact_immutable_update", "trg_message_artifact_immutable_delete",
        "trg_group_artifact_immutable_update", "trg_group_artifact_immutable_delete",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))
    op.drop_table("group_artifact")
    op.drop_table("message_artifact")
    op.drop_table("artifact")
    op.alter_column("workspace_lock_request", "tool_invocation_id", existing_type=sa.String(36), nullable=False)
