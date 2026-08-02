"""relationship-backed Plan definition and frozen participants

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_execution",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), unique=True, nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("owner_agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("owner_agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id")),
        sa.Column("predecessor_plan_id", sa.String(36), sa.ForeignKey("plan_execution.id")),
        sa.Column("goal", sa.Text(), nullable=False), sa.Column("deliverable", sa.Text(), nullable=False),
        sa.Column("definition_status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("graph_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("confirmed_at", sa.DateTime()), sa.Column("frozen_hash", sa.String(64)),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("definition_status IN ('draft','awaiting_confirmation','frozen','cancelled')", name="ck_plan_definition_status"),
    )
    op.create_table(
        "plan_work_item_node",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_execution_id", sa.String(36), sa.ForeignKey("plan_execution.id"), nullable=False),
        sa.Column("node_key", sa.String(100), nullable=False), sa.Column("title", sa.String(300), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False), sa.Column("acceptance_criteria", sa.Text(), nullable=False),
        sa.Column("assigned_agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id")),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_item.id"), unique=True),
        sa.Column("sort_order", sa.Integer(), nullable=False), sa.Column("definition_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("plan_execution_id", "node_key", name="uq_plan_node_key"),
        sa.UniqueConstraint("plan_execution_id", "sort_order", name="uq_plan_node_order"),
    )
    op.create_table(
        "plan_work_item_dependency",
        sa.Column("plan_execution_id", sa.String(36), sa.ForeignKey("plan_execution.id"), primary_key=True),
        sa.Column("predecessor_node_id", sa.String(36), sa.ForeignKey("plan_work_item_node.id"), primary_key=True),
        sa.Column("successor_node_id", sa.String(36), sa.ForeignKey("plan_work_item_node.id"), primary_key=True),
        sa.Column("dependency_type", sa.String(20), nullable=False, server_default="success"),
        sa.CheckConstraint("predecessor_node_id != successor_node_id", name="ck_plan_dependency_not_self"),
        sa.CheckConstraint("dependency_type IN ('success','settled')", name="ck_plan_dependency_type"),
    )
    op.create_table(
        "plan_declared_agent",
        sa.Column("plan_execution_id", sa.String(36), sa.ForeignKey("plan_execution.id"), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id"), primary_key=True),
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_version.id"), nullable=False),
    )
    op.execute("""
        CREATE TRIGGER trg_plan_frozen_update BEFORE UPDATE ON plan_execution FOR EACH ROW
        BEGIN
          IF OLD.definition_status = 'frozen' AND NOT (
            NEW.definition_status = 'frozen' AND OLD.id <=> NEW.id AND OLD.run_id <=> NEW.run_id
            AND OLD.session_id <=> NEW.session_id AND OLD.user_id <=> NEW.user_id
            AND OLD.owner_agent_id <=> NEW.owner_agent_id AND OLD.owner_agent_version_id <=> NEW.owner_agent_version_id
            AND OLD.predecessor_plan_id <=> NEW.predecessor_plan_id AND OLD.goal <=> NEW.goal
            AND OLD.deliverable <=> NEW.deliverable AND OLD.graph_revision <=> NEW.graph_revision
            AND OLD.confirmed_at <=> NEW.confirmed_at AND OLD.frozen_hash <=> NEW.frozen_hash
          ) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'frozen Plan definition is immutable'; END IF;
        END
    """)
    for table in ("plan_work_item_node", "plan_work_item_dependency", "plan_declared_agent"):
        plan_field = "plan_execution_id"
        for operation in ("INSERT", "UPDATE", "DELETE"):
            ref = "OLD" if operation == "DELETE" else "NEW"
            if table == "plan_work_item_node" and operation == "UPDATE":
                op.execute("""
                    CREATE TRIGGER trg_plan_work_item_node_frozen_update
                    BEFORE UPDATE ON plan_work_item_node FOR EACH ROW
                    BEGIN
                      IF EXISTS (SELECT 1 FROM plan_execution WHERE id=NEW.plan_execution_id AND definition_status='frozen')
                         AND NOT (
                           OLD.id <=> NEW.id AND OLD.plan_execution_id <=> NEW.plan_execution_id
                           AND OLD.node_key <=> NEW.node_key AND OLD.title <=> NEW.title
                           AND OLD.goal <=> NEW.goal AND OLD.acceptance_criteria <=> NEW.acceptance_criteria
                           AND OLD.assigned_agent_id <=> NEW.assigned_agent_id
                           AND OLD.agent_version_id <=> NEW.agent_version_id
                           AND OLD.sort_order <=> NEW.sort_order AND OLD.definition_version <=> NEW.definition_version
                           AND (OLD.work_item_id <=> NEW.work_item_id OR (OLD.work_item_id IS NULL AND NEW.work_item_id IS NOT NULL))
                         )
                      THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'frozen Plan graph is immutable'; END IF;
                    END
                """)
                continue
            op.execute(f"""
                CREATE TRIGGER trg_{table}_frozen_{operation.lower()} BEFORE {operation} ON {table} FOR EACH ROW
                BEGIN
                  IF EXISTS (SELECT 1 FROM plan_execution WHERE id={ref}.{plan_field} AND definition_status='frozen')
                  THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'frozen Plan graph is immutable'; END IF;
                END
            """)


def downgrade() -> None:
    for table in ("plan_work_item_node", "plan_work_item_dependency", "plan_declared_agent"):
        for operation in ("insert", "update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_frozen_{operation}")
    op.execute("DROP TRIGGER IF EXISTS trg_plan_frozen_update")
    op.drop_table("plan_declared_agent")
    op.drop_table("plan_work_item_dependency")
    op.drop_table("plan_work_item_node")
    op.drop_table("plan_execution")
