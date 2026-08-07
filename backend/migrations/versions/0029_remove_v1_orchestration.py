"""remove V1 orchestration projections after immutable fact archival

Revision ID: 0029
Revises: 0028

This is a forward-only phase-one cutover.  Any row carried by the deprecated
Completion/Plan-DAG/Formation schema is copied verbatim to an append-only
archive before its active table or column is removed.  The controlled UAT
stack is created on a fresh database; this archival path exists so an operator
cannot accidentally rewrite old failure facts when upgrading another database.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


LEGACY_TABLES = (
    "plan_execution",
    "plan_work_item_node",
    "plan_work_item_dependency",
    "plan_declared_agent",
    "group_formation_proposal",
    "group_formation_revision",
    "group_formation_participant",
    "group_formation_work_item",
    "group_formation_suggested_edge",
    "group_agent_node",
    "group_initial_suggested_edge",
    "group_run_agent_version",
    "group_collaboration_edge",
)

LEGACY_DROP_ORDER = (
    "group_collaboration_edge",
    "group_initial_suggested_edge",
    "group_run_agent_version",
    "group_agent_node",
    "group_formation_suggested_edge",
    "group_formation_work_item",
    "group_formation_participant",
    "group_formation_revision",
    "group_formation_proposal",
    "plan_work_item_dependency",
    "plan_declared_agent",
    "plan_work_item_node",
    "plan_execution",
)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(item["name"] == column for item in _inspector().get_columns(table))


def _archive_table(table: str, *, label: str | None = None) -> None:
    """Copy every source column and primary-key value without interpretation."""
    if not _has_table(table):
        return
    inspector = _inspector()
    columns = [item["name"] for item in inspector.get_columns(table)]
    if not columns:
        return
    primary = list((inspector.get_pk_constraint(table) or {}).get("constrained_columns") or [])
    if not primary:
        primary = columns
    payload = "JSON_OBJECT(" + ",".join(
        f"'{name}',`{name}`" for name in columns
    ) + ")"
    source_pk = "JSON_OBJECT(" + ",".join(
        f"'{name}',`{name}`" for name in primary
    ) + ")"
    op.execute(sa.text(
        "INSERT INTO legacy_uat_fact_archive "
        "(source_table,source_pk,payload,payload_sha256,archived_at) "
        f"SELECT :label,CAST({source_pk} AS CHAR),CAST({payload} AS CHAR),"
        f"SHA2(CAST({payload} AS CHAR),256),UTC_TIMESTAMP(6) FROM `{table}`"
    ).bindparams(label=label or table))


def _drop_foreign_keys(
    table: str, *, local_columns: set[str] | None = None,
    referred_tables: set[str] | None = None,
) -> None:
    if not _has_table(table):
        return
    for constraint in _inspector().get_foreign_keys(table):
        name = constraint.get("name")
        constrained = set(constraint.get("constrained_columns") or [])
        referred = constraint.get("referred_table")
        if not name:
            continue
        if local_columns is not None and not (constrained & local_columns):
            continue
        if referred_tables is not None and referred not in referred_tables:
            continue
        op.drop_constraint(name, table, type_="foreignkey")


def _drop_unique_for_columns(table: str, columns: set[str]) -> None:
    for constraint in _inspector().get_unique_constraints(table):
        if set(constraint.get("column_names") or []) & columns:
            name = constraint.get("name")
            if name:
                op.drop_constraint(name, table, type_="unique")


def _drop_check_if_present(table: str, name: str) -> None:
    names = {item.get("name") for item in _inspector().get_check_constraints(table)}
    if name in names:
        op.drop_constraint(name, table, type_="check")


def upgrade() -> None:
    op.create_table(
        "legacy_uat_fact_archive",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_table", sa.String(191), nullable=False),
        sa.Column("source_pk", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_legacy_uat_fact_source", "legacy_uat_fact_archive",
        ["source_table", "id"],
    )
    op.add_column("provider_attempt", sa.Column("first_payload_at", sa.DateTime()))
    op.add_column(
        "agent_turn",
        sa.Column("schema_repair_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # Capture complete rows before any compatibility column is translated.
    for table in LEGACY_TABLES:
        _archive_table(table)
    for table in ("run", "group_session", "group_run", "message", "work_item",
                  "clarification", "history_summary", "group_artifact"):
        _archive_table(table, label=f"{table}:pre_v2_cutover")

    # Add membership-native principal columns and retain only values that map
    # to a real Agent-network membership.  Unmapped V1 values remain in the
    # immutable archive, never masquerading as a current principal.
    op.add_column("message", sa.Column("recipient_membership_id", sa.String(36)))
    op.add_column("work_item", sa.Column("group_membership_id", sa.String(36)))
    op.add_column("clarification", sa.Column("group_membership_id", sa.String(36)))
    op.add_column("history_summary", sa.Column("group_membership_id", sa.String(36)))
    op.execute(sa.text(
        "UPDATE message m JOIN group_agent_node n ON n.id=m.recipient_group_node_id "
        "JOIN group_membership gm ON gm.group_session_id=n.group_session_id AND gm.agent_id=n.agent_id "
        "SET m.recipient_membership_id=gm.id"
    ))
    op.execute(sa.text(
        "UPDATE work_item w JOIN group_agent_node n ON n.id=w.group_node_id "
        "JOIN group_membership gm ON gm.group_session_id=n.group_session_id AND gm.agent_id=n.agent_id "
        "SET w.group_membership_id=gm.id"
    ))
    op.execute(sa.text(
        "UPDATE clarification c JOIN group_agent_node n ON n.id=c.group_node_id "
        "JOIN group_membership gm ON gm.group_session_id=n.group_session_id AND gm.agent_id=n.agent_id "
        "SET c.group_membership_id=gm.id"
    ))
    op.execute(sa.text(
        "UPDATE history_summary h JOIN group_agent_node n ON n.id=h.group_node_id "
        "JOIN group_membership gm ON gm.group_session_id=n.group_session_id AND gm.agent_id=n.agent_id "
        "SET h.group_membership_id=gm.id"
    ))
    op.execute(sa.text(
        "UPDATE group_artifact ga JOIN group_agent_node n ON n.id=ga.published_by_node_id "
        "JOIN group_membership gm ON gm.group_session_id=ga.group_session_id AND gm.agent_id=n.agent_id "
        "SET ga.published_by_membership_id=gm.id "
        "WHERE ga.published_by_membership_id IS NULL"
    ))

    # These compatibility triggers name columns removed below.
    op.execute("DROP TRIGGER IF EXISTS trg_group_session_definition_frozen")
    op.execute("DROP TRIGGER IF EXISTS trg_group_run_immutable_update")

    for table, column in (
        ("message", "recipient_membership_id"),
        ("work_item", "group_membership_id"),
        ("clarification", "group_membership_id"),
        ("history_summary", "group_membership_id"),
    ):
        op.create_foreign_key(
            f"fk_{table}_{column}", table, "group_membership", [column], ["id"]
        )

    # History's generated discriminator and checks referred to GroupAgentNode.
    # Keep an explicit parent-key index while replacing indexes whose leading
    # session_id MySQL was also using to enforce the Session foreign key.
    op.create_index(
        "ix_history_summary_session_cutover", "history_summary", ["session_id"]
    )
    op.drop_index("ix_history_summary_ready", table_name="history_summary")
    op.drop_constraint(
        "uq_history_summary_scope_sequence", "history_summary", type_="unique"
    )
    _drop_check_if_present("history_summary", "ck_history_summary_scope_fields")
    op.drop_column("history_summary", "scope_discriminator")
    _drop_foreign_keys("history_summary", local_columns={"group_node_id"})
    op.drop_column("history_summary", "group_node_id")
    op.execute(
        "DELETE FROM history_summary "
        "WHERE scope_type='group_agent' AND group_membership_id IS NULL"
    )
    op.add_column(
        "history_summary",
        sa.Column(
            "scope_discriminator", sa.String(191),
            sa.Computed(
                "CASE WHEN scope_type='chat' THEN 'chat' "
                "ELSE CONCAT('group_membership:',group_membership_id) END",
                persisted=True,
            ),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_history_summary_scope_fields", "history_summary",
        "(scope_type='chat' AND group_session_id IS NULL AND group_membership_id IS NULL) OR "
        "(scope_type='group_agent' AND group_session_id IS NOT NULL AND group_membership_id IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_history_summary_scope_sequence", "history_summary",
        ["session_id", "scope_discriminator", "through_event_sequence"],
    )
    op.create_index(
        "ix_history_summary_ready", "history_summary",
        ["session_id", "scope_discriminator", "status", "through_event_sequence"],
    )
    op.drop_index("ix_history_summary_session_cutover", table_name="history_summary")

    # Remove all remaining active references into V1 tables.
    for table in _inspector().get_table_names():
        if table not in LEGACY_TABLES:
            _drop_foreign_keys(table, referred_tables=set(LEGACY_TABLES))

    _drop_foreign_keys("group_session", local_columns={"entry_agent_id"})
    _drop_unique_for_columns("group_session", {"frozen_formation_revision_id"})
    _drop_foreign_keys("group_run", local_columns={"owner_node_id"})
    _drop_foreign_keys("group_artifact", local_columns={"published_by_node_id"})
    _drop_check_if_present("group_artifact", "ck_group_artifact_publisher_scope")
    _drop_foreign_keys("clarification", local_columns={"group_node_id"})

    for table, columns in (
        ("message", ("recipient_group_node_id",)),
        ("work_item", (
            "plan_node_id", "group_node_id", "group_formation_proposal_id",
            "group_formation_work_item_id",
        )),
        ("clarification", ("group_node_id",)),
        ("group_session", (
            "frozen_formation_revision_id", "entry_agent_id", "confirmed_at",
        )),
        ("group_run", ("owner_node_id",)),
        ("group_artifact", ("published_by_node_id",)),
    ):
        for column in columns:
            if _has_column(table, column):
                op.drop_column(table, column)

    op.execute(
        "DELETE FROM group_artifact WHERE published_by_membership_id IS NULL"
    )
    op.alter_column(
        "group_artifact", "published_by_membership_id",
        existing_type=sa.String(36), nullable=False,
    )

    # `run.status` becomes the one physical lifecycle/terminal authority.
    op.execute(sa.text("""
        UPDATE run SET
          status = CASE
            WHEN lifecycle='completed' AND outcome='failed' THEN 'failed'
            WHEN lifecycle='completed' THEN 'completed'
            WHEN lifecycle='cancelled' THEN 'cancelled'
            WHEN lifecycle='waiting' AND wait_reason IN
              ('waiting_user','waiting_approval','waiting_tool','waiting_agent')
              THEN wait_reason
            WHEN lifecycle='waiting' AND wait_reason='clarification' THEN 'waiting_user'
            WHEN lifecycle='waiting' AND wait_reason='approval' THEN 'waiting_approval'
            ELSE status
          END,
          wait_reason = CASE
            WHEN lifecycle='waiting' AND wait_reason IN
              ('waiting_user','waiting_approval','waiting_tool','waiting_agent')
              THEN wait_reason
            WHEN lifecycle='waiting' AND wait_reason='clarification' THEN 'waiting_user'
            WHEN lifecycle='waiting' AND wait_reason='approval' THEN 'waiting_approval'
            ELSE NULL
          END,
          terminal_reason = CASE
            WHEN lifecycle='completed' AND outcome='failed'
              THEN COALESCE(terminal_reason,error_code,'failed')
            WHEN lifecycle='completed' THEN COALESCE(terminal_reason,'completed')
            WHEN lifecycle='cancelled' THEN COALESCE(terminal_reason,error_code,'cancelled')
            ELSE terminal_reason
          END,
          completed_at = CASE
            WHEN lifecycle IN ('completed','cancelled')
              THEN COALESCE(completed_at,finished_at,updated_at,created_at)
            ELSE NULL
          END
    """))
    for name in (
        "ck_run_kind", "ck_run_lifecycle", "ck_run_outcome",
        "ck_run_wait_reason", "ck_run_completed_outcome", "ck_run_v2_status",
    ):
        _drop_check_if_present("run", name)
    op.create_index("ix_run_user_id", "run", ["user_id"])
    if "ix_run_user_lifecycle" in {
        item["name"] for item in _inspector().get_indexes("run")
    }:
        op.drop_index("ix_run_user_lifecycle", table_name="run")
    for column in ("execution_kind", "lifecycle", "outcome"):
        if _has_column("run", column):
            op.drop_column("run", column)
    op.create_check_constraint(
        "ck_run_status", "run",
        "status IN ('active','waiting_user','waiting_approval','waiting_tool','waiting_agent','completed','cancelled','failed')",
    )
    op.create_check_constraint(
        "ck_run_wait_reason", "run",
        "(status IN ('waiting_user','waiting_approval','waiting_tool','waiting_agent') AND wait_reason=status) OR "
        "(status NOT IN ('waiting_user','waiting_approval','waiting_tool','waiting_agent') AND wait_reason IS NULL)",
    )
    op.create_check_constraint(
        "ck_run_terminal_time", "run",
        "(status IN ('completed','cancelled','failed') AND completed_at IS NOT NULL) OR "
        "(status NOT IN ('completed','cancelled','failed') AND completed_at IS NULL)",
    )
    op.create_index("ix_run_status_updated", "run", ["status", "updated_at", "id"])
    op.alter_column(
        "run", "permission_mode", existing_type=sa.String(32),
        nullable=False, server_default="direct",
    )
    _drop_foreign_keys(
        "run", local_columns={"entry_agent_id", "source_run_id"}
    )
    for column in (
        "entry_agent_id", "source_run_id", "result_summary", "error_code",
        "started_at", "finished_at",
    ):
        if _has_column("run", column):
            op.drop_column("run", column)

    _drop_check_if_present("group_session", "ck_group_session_status")
    op.alter_column(
        "group_session", "parent_session_id", new_column_name="session_id",
        existing_type=sa.String(36), nullable=False,
    )
    op.execute(
        "ALTER TABLE group_session RENAME INDEX "
        "ix_group_session_parent_status TO ix_group_session_session_status"
    )
    op.execute("UPDATE group_session SET status='archived' WHERE status='forming'")
    op.create_check_constraint(
        "ck_group_session_status", "group_session",
        "status IN ('open','closed','archived')",
    )
    op.alter_column(
        "group_session", "initiator_agent_id",
        existing_type=sa.String(36), nullable=False,
    )

    for table in LEGACY_DROP_ORDER:
        if _has_table(table):
            op.drop_table(table)

    op.execute(sa.text(
        "CREATE TRIGGER trg_legacy_uat_fact_archive_immutable_update "
        "BEFORE UPDATE ON legacy_uat_fact_archive FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'legacy UAT facts are append-only'"
    ))
    op.execute(sa.text(
        "CREATE TRIGGER trg_legacy_uat_fact_archive_immutable_delete "
        "BEFORE DELETE ON legacy_uat_fact_archive FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'legacy UAT facts are append-only'"
    ))
    op.execute(sa.text(
        "CREATE TRIGGER trg_legacy_uat_fact_archive_immutable_insert "
        "BEFORE INSERT ON legacy_uat_fact_archive FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'legacy UAT archive is sealed'"
    ))


def downgrade() -> None:
    raise RuntimeError(
        "0029 is a forward-only cutover; legacy facts remain in legacy_uat_fact_archive"
    )
