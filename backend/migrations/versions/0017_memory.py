"""versioned Memory heads, candidates, sources, and recall snapshots

Revision ID: 0017
Revises: 0016
"""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    ]


def immutable_relation(table):
    op.execute(sa.text(f"CREATE TRIGGER trg_{table}_immutable_update BEFORE UPDATE ON {table} FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is immutable'"))
    op.execute(sa.text(f"CREATE TRIGGER trg_{table}_immutable_delete BEFORE DELETE ON {table} FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is immutable'"))


def upgrade():
    op.create_table(
        "memory_head",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id")),
        sa.Column("fact_key", sa.String(255), nullable=False),
        sa.Column("scope_discriminator", sa.String(64), sa.Computed(
            "IF(scope = 'user', 'user', CONCAT('user_agent:', agent_id))", persisted=True), nullable=False),
        sa.Column("last_user_edited_at", sa.DateTime()),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *timestamps(),
        sa.CheckConstraint("(scope = 'user' AND agent_id IS NULL) OR (scope = 'user_agent' AND agent_id IS NOT NULL)", name="ck_memory_head_scope_agent"),
        sa.UniqueConstraint("user_id", "scope_discriminator", "fact_key", name="uq_memory_head_logical_key"),
    )
    op.create_table(
        "memory",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("head_id", sa.String(36), sa.ForeignKey("memory_head.id"), nullable=False),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id")),
        sa.Column("scope", sa.String(20), nullable=False), sa.Column("fact_key", sa.String(255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False), sa.Column("normalized_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("active_head_id", sa.String(36), sa.Computed("IF(status = 'active', head_id, NULL)", persisted=True)),
        sa.Column("source_kind", sa.String(20), nullable=False),
        sa.Column("source_session_id", sa.String(36), sa.ForeignKey("user_session.id")),
        sa.Column("created_by_runner_id", sa.String(36), sa.ForeignKey("runner_v1.id")),
        sa.Column("policy_definition_id", sa.String(36), sa.ForeignKey("policy_definition.id")),
        sa.Column("confidence", sa.Numeric(5, 4)),
        sa.Column("supersedes_memory_id", sa.String(36), sa.ForeignKey("memory.id")),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *timestamps(),
        sa.CheckConstraint("scope IN ('user','user_agent','agent')", name="ck_memory_scope"),
        sa.CheckConstraint("status IN ('active','superseded','deleted')", name="ck_memory_status"),
        sa.CheckConstraint("source_kind IN ('model','user_edit','import')", name="ck_memory_source_kind"),
        sa.UniqueConstraint("active_head_id", name="uq_memory_one_active"),
    )
    op.create_index("ix_memory_user_scope_status_updated", "memory", ["user_id", "scope", "agent_id", "status", "updated_at"])
    op.create_table(
        "memory_source_message",
        sa.Column("memory_id", sa.String(36), sa.ForeignKey("memory.id"), primary_key=True),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("message.id"), primary_key=True),
        sa.Column("source_role", sa.String(20), nullable=False),
    )
    op.create_table(
        "runner_memory_snapshot",
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), primary_key=True),
        sa.Column("memory_id", sa.String(36), sa.ForeignKey("memory.id"), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False), sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False), sa.Column("score", sa.Numeric(8, 4), nullable=False),
    )
    op.create_table(
        "memory_write_candidate",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id")), sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("fact_key", sa.String(255), nullable=False), sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_hash", sa.String(64), nullable=False),
        sa.Column("source_session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), nullable=False),
        sa.Column("policy_definition_id", sa.String(36), sa.ForeignKey("policy_definition.id"), nullable=False),
        sa.Column("observed_head_version", sa.BigInteger()), sa.Column("confidence", sa.Numeric(5, 4)),
        sa.Column("reason", sa.Text()), sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("rejection_code", sa.String(100)),
        sa.Column("result_memory_id", sa.String(36), sa.ForeignKey("memory.id")),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *timestamps(),
        sa.CheckConstraint("scope IN ('user','user_agent')", name="ck_memory_candidate_scope"),
        sa.CheckConstraint("status IN ('pending','accepted','rejected','failed')", name="ck_memory_candidate_status"),
    )
    op.create_index("ix_memory_candidate_status_created", "memory_write_candidate", ["status", "created_at"])
    op.create_table(
        "memory_candidate_source_message",
        sa.Column("candidate_id", sa.String(36), sa.ForeignKey("memory_write_candidate.id"), primary_key=True),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("message.id"), primary_key=True),
        sa.Column("source_role", sa.String(20), nullable=False),
    )
    op.execute(sa.text("""
        CREATE TRIGGER trg_memory_content_immutable_update BEFORE UPDATE ON memory FOR EACH ROW
        BEGIN
          IF NOT (NEW.id <=> OLD.id AND NEW.head_id <=> OLD.head_id AND NEW.user_id <=> OLD.user_id
            AND NEW.agent_id <=> OLD.agent_id AND NEW.scope <=> OLD.scope AND NEW.fact_key <=> OLD.fact_key
            AND NEW.content <=> OLD.content AND NEW.normalized_hash <=> OLD.normalized_hash
            AND NEW.source_kind <=> OLD.source_kind AND NEW.source_session_id <=> OLD.source_session_id
            AND NEW.created_by_runner_id <=> OLD.created_by_runner_id
            AND NEW.policy_definition_id <=> OLD.policy_definition_id AND NEW.confidence <=> OLD.confidence
            AND NEW.supersedes_memory_id <=> OLD.supersedes_memory_id AND NEW.created_at <=> OLD.created_at) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Memory content version is immutable';
          END IF;
          IF OLD.status <> 'active' AND NEW.status <> OLD.status THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Memory terminal version cannot transition';
          END IF;
        END
    """))
    op.execute(sa.text("CREATE TRIGGER trg_memory_immutable_delete BEFORE DELETE ON memory FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Memory version is immutable'"))
    for table in ("memory_source_message", "runner_memory_snapshot", "memory_candidate_source_message"):
        immutable_relation(table)


def downgrade():
    for trigger in (
        "trg_memory_content_immutable_update", "trg_memory_immutable_delete",
        "trg_memory_source_message_immutable_update", "trg_memory_source_message_immutable_delete",
        "trg_runner_memory_snapshot_immutable_update", "trg_runner_memory_snapshot_immutable_delete",
        "trg_memory_candidate_source_message_immutable_update", "trg_memory_candidate_source_message_immutable_delete",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))
    for table in ("memory_candidate_source_message", "memory_write_candidate", "runner_memory_snapshot", "memory_source_message", "memory", "memory_head"):
        op.drop_table(table)
