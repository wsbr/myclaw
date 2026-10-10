"""Allow only audited, expired terminal checkpoint retention batches.

Revision ID: 0050
Revises: 0049
"""

from alembic import op
import sqlalchemy as sa


revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def _create_if_missing(name: str, statement: str) -> None:
    exists = op.get_bind().scalar(sa.text(
        "SELECT COUNT(*) FROM information_schema.TRIGGERS "
        "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=:name"
    ), {"name": name})
    if not exists:
        op.execute(sa.text(statement))


def upgrade() -> None:
    # Protect the audit allowlists as append-only facts. These operational
    # guards complement, but do not replace, separate retention DB privileges.
    for event in ("UPDATE", "DELETE"):
        condition = "OLD.action = 'retention.checkpoint_batch'"
        if event == "UPDATE":
            condition += " OR NEW.action = 'retention.checkpoint_batch'"
        name = f"trg_retention_batch_audit_{event.lower()}"
        _create_if_missing(name, f"""
            CREATE TRIGGER {name} BEFORE {event} ON platform_audit_event FOR EACH ROW
            BEGIN
              IF {condition} THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'retention batch audit is append-only';
              END IF;
            END
        """)
    # Install the stricter bounded exception before removing the old guard.
    # Interrupted MySQL DDL must never leave checkpoints without protection.
    _create_if_missing("trg_runner_checkpoint_retention_delete", """
        CREATE TRIGGER trg_runner_checkpoint_retention_delete
        BEFORE DELETE ON runner_checkpoint FOR EACH ROW
        BEGIN
          IF COALESCE(IS_USED_LOCK('myclaw.retention_cleanup'), 0) <> CONNECTION_ID()
             OR NOT EXISTS (
               SELECT 1 FROM platform_audit_event a
               JOIN work_item w ON w.id = OLD.work_item_id
               JOIN run r ON r.id = w.run_id
               WHERE a.category = 'maintenance'
                 AND a.action = 'retention.checkpoint_batch'
                 AND a.actor_type = 'service' AND a.actor_id = 'maintenance-worker'
                 AND a.outcome = 'succeeded' AND a.resource_type = 'retention_job'
                 AND a.occurred_at >= DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 5 MINUTE)
                 AND CAST(JSON_UNQUOTE(JSON_EXTRACT(a.metadata, '$.connection_id')) AS UNSIGNED)
                     = CONNECTION_ID()
                 AND JSON_CONTAINS(JSON_EXTRACT(a.changes, '$.checkpoint_ids'), JSON_QUOTE(OLD.id))
                 AND CAST(JSON_UNQUOTE(JSON_EXTRACT(a.metadata, '$.cutoff_at')) AS DATETIME(6))
                     <= DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 30 DAY)
                 AND OLD.created_at < CAST(JSON_UNQUOTE(JSON_EXTRACT(a.metadata, '$.cutoff_at')) AS DATETIME(6))
                 AND w.updated_at < CAST(JSON_UNQUOTE(JSON_EXTRACT(a.metadata, '$.cutoff_at')) AS DATETIME(6))
                 AND w.status IN ('succeeded','failed','timed_out','cancelled','abandoned','blocked')
                 AND r.status IN ('completed','failed','cancelled')
                 AND r.completed_at < CAST(JSON_UNQUOTE(JSON_EXTRACT(a.metadata, '$.cutoff_at')) AS DATETIME(6))
             ) THEN
            SIGNAL SQLSTATE '45000'
              SET MESSAGE_TEXT = 'runner_checkpoint is append-only outside audited retention';
          END IF;
        END
    """)
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_runner_checkpoint_immutable_delete"))


def downgrade() -> None:
    _create_if_missing("trg_runner_checkpoint_immutable_delete", """
        CREATE TRIGGER trg_runner_checkpoint_immutable_delete
        BEFORE DELETE ON runner_checkpoint FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'runner_checkpoint is append-only'
    """)
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_runner_checkpoint_retention_delete"))
    for event in ("update", "delete"):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_retention_batch_audit_{event}"))
