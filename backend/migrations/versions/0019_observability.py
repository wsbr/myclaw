"""Runner traces, spans, links, and final immutable ledgers

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"; down_revision = "0018"; branch_labels = None; depends_on = None

def upgrade():
    op.create_table("runner_trace",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("trace_id", sa.String(32), nullable=False, unique=True),
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner_v1.id"), nullable=False, unique=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"), sa.Column("sampled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error_code", sa.String(100)), sa.Column("started_at", sa.DateTime(), nullable=False), sa.Column("ended_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("status IN ('running','completed','failed','cancelled')", name="ck_runner_trace_status"))
    op.create_index("ix_runner_trace_run_created", "runner_trace", ["run_id", "created_at"])
    op.create_table("observability_span",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("runner_trace_id", sa.String(36), sa.ForeignKey("runner_trace.id"), nullable=False),
        sa.Column("span_id", sa.String(16), nullable=False, unique=True), sa.Column("parent_span_id", sa.String(16)),
        sa.Column("name", sa.String(200), nullable=False), sa.Column("span_kind", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False), sa.Column("started_at", sa.DateTime(), nullable=False), sa.Column("ended_at", sa.DateTime(), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False), sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("input_tokens", sa.Integer()), sa.Column("output_tokens", sa.Integer()), sa.Column("error_code", sa.String(100)),
        sa.Column("input_summary", sa.Text()), sa.Column("output_summary", sa.Text()),
        sa.CheckConstraint("status IN ('ok','error')", name="ck_observability_span_status"))
    op.create_index("ix_observability_span_trace_started", "observability_span", ["runner_trace_id", "started_at"])
    op.create_table("observability_span_link",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("span_id", sa.String(36), sa.ForeignKey("observability_span.id"), nullable=False),
        sa.Column("linked_trace_id", sa.String(32), nullable=False), sa.Column("linked_span_id", sa.String(16)),
        sa.Column("link_type", sa.String(50), nullable=False), sa.Column("entity_type", sa.String(64)), sa.Column("entity_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("span_id", "linked_trace_id", "linked_span_id", "link_type", name="uq_observability_span_link"))
    op.execute(sa.text("CREATE TRIGGER trg_execution_event_immutable_update BEFORE UPDATE ON execution_event FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'ExecutionEvent is append-only'"))
    op.execute(sa.text("CREATE TRIGGER trg_execution_event_immutable_delete BEFORE DELETE ON execution_event FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'ExecutionEvent is append-only'"))
    op.execute(sa.text("""
        CREATE TRIGGER trg_group_session_definition_frozen BEFORE UPDATE ON group_session FOR EACH ROW
        BEGIN
          IF OLD.frozen_formation_revision_id IS NOT NULL AND NOT (
            NEW.id <=> OLD.id AND NEW.parent_session_id <=> OLD.parent_session_id AND NEW.user_id <=> OLD.user_id
            AND NEW.frozen_formation_revision_id <=> OLD.frozen_formation_revision_id
            AND NEW.entry_agent_id <=> OLD.entry_agent_id AND NEW.goal <=> OLD.goal
            AND NEW.bootstrap_context <=> OLD.bootstrap_context AND NEW.confirmed_at <=> OLD.confirmed_at
            AND NEW.created_at <=> OLD.created_at) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Confirmed GroupSession definition is frozen';
          END IF;
        END
    """))

def downgrade():
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_group_session_definition_frozen"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_execution_event_immutable_delete"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_execution_event_immutable_update"))
    op.drop_table("observability_span_link"); op.drop_table("observability_span"); op.drop_table("runner_trace")
