"""Persist the minimum Tenant authority and backfill existing local data.

Revision ID: 0038
Revises: 0037
"""

from alembic import op
import sqlalchemy as sa


revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


DEFAULT_TENANT_ID = "00000000-0000-7000-8000-000000000001"


TENANT_COLUMNS = (
    "agent_api_key",
    "agent",
    "user_session",
    "run",
    "work_item",
    "runner_v1",
    "workspace",
    "tool_invocation",
    "agent_invocation",
    "group_session",
    "memory_head",
    "memory",
    "memory_write_candidate",
    "artifact",
    "channel_account",
    "usage_ledger_entry",
)


TENANT_INDEXES = {
    "platform_account": ("ix_platform_account_tenant_status", ["tenant_id", "status", "created_at"]),
    "agent_api_key": ("ix_agent_api_key_tenant_status", ["tenant_id", "status"]),
    "agent": ("ix_agent_tenant_visibility_status", ["tenant_id", "visibility", "status"]),
    "user_session": ("ix_user_session_tenant_account", ["tenant_id", "account_id", "updated_at"]),
    "run": ("ix_run_tenant_account_created", ["tenant_id", "account_id", "created_at"]),
    "workspace": ("ix_workspace_tenant_session", ["tenant_id", "session_id"]),
    "tool_invocation": ("ix_tool_invocation_tenant_account", ["tenant_id", "account_id", "created_at"]),
    "agent_invocation": ("ix_agent_invocation_tenant_account", ["tenant_id", "account_id", "created_at"]),
    "group_session": ("ix_group_session_tenant_status", ["tenant_id", "status", "created_at"]),
    "memory_head": ("ix_memory_head_tenant_user", ["tenant_id", "user_id", "updated_at"]),
    "memory": ("ix_memory_tenant_user_status", ["tenant_id", "user_id", "status", "updated_at"]),
    "memory_write_candidate": ("ix_memory_candidate_tenant_status", ["tenant_id", "status", "created_at"]),
    "artifact": ("ix_artifact_tenant_session", ["tenant_id", "session_id", "created_at"]),
    "channel_account": ("ix_channel_account_tenant_status", ["tenant_id", "status"]),
    "usage_ledger_entry": ("ix_usage_entry_tenant_created", ["tenant_id", "created_at", "id"]),
    "platform_audit_event": ("ix_platform_audit_tenant_occurred", ["tenant_id", "occurred_at", "id"]),
    "outbox_command": ("ix_outbox_tenant_claim", ["tenant_id", "topic", "status", "available_at"]),
}


# These constraints make the Tenant column part of aggregate identity at the
# database layer.  The original scalar foreign keys remain useful for legacy
# joins; these composite keys reject a child that points at a real parent ID in
# another Tenant.
TENANT_COMPOSITE_FKS = (
    ("fk_agent_owner_tenant_v1", "agent", "platform_account", ["owner_account_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_agent_api_key_agent_tenant_v1", "agent_api_key", "agent", ["agent_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_user_session_account_tenant_v1", "user_session", "platform_account", ["account_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_run_session_tenant_v1", "run", "user_session", ["session_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_run_account_tenant_v1", "run", "platform_account", ["account_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_work_item_run_tenant_v1", "work_item", "run", ["run_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_runner_work_tenant_v1", "runner_v1", "work_item", ["work_item_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_runner_run_tenant_v1", "runner_v1", "run", ["run_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_workspace_session_tenant_v1", "workspace", "user_session", ["session_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_tool_invocation_account_tenant_v1", "tool_invocation", "platform_account", ["account_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_tool_invocation_run_tenant_v1", "tool_invocation", "run", ["run_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_tool_invocation_work_tenant_v1", "tool_invocation", "work_item", ["work_item_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_tool_invocation_runner_tenant_v1", "tool_invocation", "runner_v1", ["runner_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_agent_invocation_account_tenant_v1", "agent_invocation", "platform_account", ["account_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_agent_invocation_run_tenant_v1", "agent_invocation", "run", ["run_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_agent_invocation_caller_work_tenant_v1", "agent_invocation", "work_item", ["caller_work_item_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_agent_invocation_target_work_tenant_v1", "agent_invocation", "work_item", ["target_work_item_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_group_session_session_tenant_v1", "group_session", "user_session", ["session_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_memory_record_head_tenant_v1", "memory", "memory_head", ["head_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_memory_candidate_session_tenant_v1", "memory_write_candidate", "user_session", ["source_session_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_memory_candidate_runner_tenant_v1", "memory_write_candidate", "runner_v1", ["runner_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_artifact_session_tenant_v1", "artifact", "user_session", ["session_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_artifact_workspace_tenant_v1", "artifact", "workspace", ["workspace_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_channel_account_owner_tenant_v1", "channel_account", "platform_account", ["owner_account_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_usage_account_tenant_v1", "usage_ledger_entry", "platform_account", ["account_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_usage_session_tenant_v1", "usage_ledger_entry", "user_session", ["session_id", "tenant_id"], ["id", "tenant_id"]),
    ("fk_usage_run_tenant_v1", "usage_ledger_entry", "run", ["run_id", "tenant_id"], ["id", "tenant_id"]),
)


def _tenant_fk_name(table: str) -> str:
    return f"fk_{table}_tenant_v1"


def _inspector():
    # MySQL DDL is non-transactional. Rebuild the Inspector for every probe so
    # a retry sees objects committed before a process/container interruption.
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return _inspector().has_table(table)


def _has_column(table: str, column: str) -> bool:
    return any(item["name"] == column for item in _inspector().get_columns(table))


def _has_index(table: str, name: str) -> bool:
    return any(item["name"] == name for item in _inspector().get_indexes(table))


def _has_constraint(table: str, name: str) -> bool:
    inspector = _inspector()
    candidates = (
        inspector.get_foreign_keys(table)
        + inspector.get_unique_constraints(table)
        + inspector.get_check_constraints(table)
    )
    return any(item.get("name") == name for item in candidates)


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    if not _has_index(table, name):
        op.create_index(name, table, columns)


def _create_unique_if_missing(name: str, table: str, columns: list[str]) -> None:
    if not _has_constraint(table, name):
        op.create_unique_constraint(name, table, columns)


def _create_fk_if_missing(
    name: str, child: str, parent: str,
    local_columns: list[str], remote_columns: list[str],
) -> None:
    if not _has_constraint(child, name):
        op.create_foreign_key(
            name, child, parent, local_columns, remote_columns,
        )


def _drop_backfill_update_guards() -> None:
    # These older guards reject every UPDATE to terminal/append-only rows,
    # including an expand/backfill of new authority columns. The migration job
    # is the only writer while upgrading; recreate the exact guards immediately
    # after the bounded authority backfill. IF EXISTS also makes crash recovery
    # safe when a prior attempt stopped between DROP and CREATE.
    for name in (
        "trg_run_terminal_update",
        "trg_tool_invocation_terminal_update",
        "trg_usage_ledger_entry_immutable_update",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _restore_backfill_update_guards() -> None:
    op.execute(sa.text("""
        CREATE TRIGGER trg_run_terminal_update
        BEFORE UPDATE ON run FOR EACH ROW
        BEGIN
          IF OLD.status IN ('completed','cancelled','failed') THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'terminal Run is immutable';
          END IF;
        END
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_tool_invocation_terminal_update
        BEFORE UPDATE ON tool_invocation FOR EACH ROW
        BEGIN
          IF OLD.status IN ('succeeded','failed','rejected','cancelled','outcome_unknown') THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'terminal ToolInvocation is immutable';
          END IF;
        END
    """))
    op.execute(
        "CREATE TRIGGER trg_usage_ledger_entry_immutable_update "
        "BEFORE UPDATE ON usage_ledger_entry FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'usage ledger fact is append-only'"
    )


def _add_tenant_column(table: str) -> None:
    if not _has_column(table, "tenant_id"):
        op.add_column(table, sa.Column("tenant_id", sa.String(36), nullable=True))


def _enforce_tenant_column(table: str) -> None:
    op.alter_column(table, "tenant_id", existing_type=sa.String(36), nullable=False)
    _create_fk_if_missing(
        _tenant_fk_name(table), table, "tenant", ["tenant_id"], ["id"]
    )


def upgrade() -> None:
    if not _has_table("tenant"):
        op.create_table(
            "tenant",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("slug", sa.String(100), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("version", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("status IN ('active','disabled')", name="ck_tenant_status"),
            sa.UniqueConstraint("slug", name="uq_tenant_slug"),
        )
    _create_index_if_missing(
        "ix_tenant_status_created", "tenant", ["status", "created_at"],
    )
    op.execute(sa.text(
        "INSERT INTO tenant (id, slug, name, status, version, created_at, updated_at) "
        f"SELECT '{DEFAULT_TENANT_ID}', 'default', 'Default Tenant', 'active', 1, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP WHERE NOT EXISTS "
        f"(SELECT 1 FROM tenant WHERE id = '{DEFAULT_TENANT_ID}')"
    ))

    if not _has_column("platform_account", "tenant_id"):
        op.add_column("platform_account", sa.Column("tenant_id", sa.String(36), nullable=True))
    op.execute(sa.text(
        f"UPDATE platform_account SET tenant_id = '{DEFAULT_TENANT_ID}' WHERE tenant_id IS NULL"
    ))
    op.alter_column("platform_account", "tenant_id", existing_type=sa.String(36), nullable=False)
    _create_fk_if_missing(
        "fk_platform_account_tenant_v1", "platform_account", "tenant",
        ["tenant_id"], ["id"],
    )
    _create_unique_if_missing(
        "uq_platform_account_id_tenant", "platform_account", ["id", "tenant_id"]
    )

    for table in TENANT_COLUMNS:
        _add_tenant_column(table)
    for table in ("run", "tool_invocation", "agent_invocation"):
        if not _has_column(table, "account_id"):
            op.add_column(table, sa.Column("account_id", sa.String(36), nullable=True))
    if not _has_column("platform_audit_event", "tenant_id"):
        op.add_column("platform_audit_event", sa.Column("tenant_id", sa.String(36), nullable=True))
    for column in ("tenant_id", "account_id"):
        if not _has_column("outbox_command", column):
            op.add_column("outbox_command", sa.Column(column, sa.String(36), nullable=True))

    _drop_backfill_update_guards()

    # Complete the old account expand/contract before Tenant enforcement.
    op.execute(sa.text(
        "INSERT INTO platform_account "
        "(id, tenant_id, user_id, email, display_name, password_hash, status, "
        "auth_revision, version, created_at, updated_at) "
        "SELECT UUID(), '" + DEFAULT_TENANT_ID + "', ui.user_id, NULL, ui.user_id, NULL, "
        "'active', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
        "FROM user_identity ui "
        "JOIN (SELECT DISTINCT user_id FROM user_session) s ON s.user_id = ui.user_id "
        "LEFT JOIN platform_account pa ON pa.user_id = ui.user_id "
        "WHERE pa.id IS NULL"
    ))
    op.execute(sa.text(
        "UPDATE user_session s JOIN platform_account a ON a.user_id = s.user_id "
        "SET s.account_id = a.id WHERE s.account_id IS NULL"
    ))

    op.execute(sa.text("UPDATE agent a JOIN platform_account p ON p.id = a.owner_account_id SET a.tenant_id = p.tenant_id"))
    op.execute(sa.text("UPDATE agent_api_key k JOIN agent a ON a.id = k.agent_id SET k.tenant_id = a.tenant_id"))
    op.execute(sa.text("UPDATE user_session s JOIN platform_account a ON a.id = s.account_id SET s.tenant_id = a.tenant_id"))
    op.execute(sa.text("UPDATE run r JOIN user_session s ON s.id = r.session_id SET r.tenant_id = s.tenant_id, r.account_id = s.account_id"))
    op.execute(sa.text("UPDATE work_item w JOIN run r ON r.id = w.run_id SET w.tenant_id = r.tenant_id"))
    op.execute(sa.text("UPDATE runner_v1 x JOIN run r ON r.id = x.run_id SET x.tenant_id = r.tenant_id"))
    op.execute(sa.text("UPDATE workspace w JOIN user_session s ON s.id = w.session_id SET w.tenant_id = s.tenant_id"))
    op.execute(sa.text("UPDATE tool_invocation i JOIN run r ON r.id = i.run_id SET i.tenant_id = r.tenant_id, i.account_id = r.account_id"))
    op.execute(sa.text("UPDATE agent_invocation i JOIN run r ON r.id = i.run_id SET i.tenant_id = r.tenant_id, i.account_id = r.account_id"))
    op.execute(sa.text("UPDATE group_session g JOIN user_session s ON s.id = g.session_id SET g.tenant_id = s.tenant_id"))
    op.execute(sa.text(
        "UPDATE memory_head h LEFT JOIN platform_account a ON a.user_id = h.user_id "
        f"SET h.tenant_id = COALESCE(a.tenant_id, '{DEFAULT_TENANT_ID}')"
    ))
    op.execute(sa.text("UPDATE memory m JOIN memory_head h ON h.id = m.head_id SET m.tenant_id = h.tenant_id"))
    op.execute(sa.text("UPDATE memory_write_candidate c JOIN user_session s ON s.id = c.source_session_id SET c.tenant_id = s.tenant_id"))
    op.execute(sa.text("UPDATE artifact a JOIN user_session s ON s.id = a.session_id SET a.tenant_id = s.tenant_id"))
    op.execute(sa.text("UPDATE channel_account c JOIN platform_account a ON a.id = c.owner_account_id SET c.tenant_id = a.tenant_id"))
    op.execute(sa.text("UPDATE usage_ledger_entry u JOIN platform_account a ON a.id = u.account_id SET u.tenant_id = a.tenant_id"))
    op.execute(sa.text(
        "UPDATE platform_audit_event e JOIN platform_account a ON a.id = e.actor_id "
        "SET e.tenant_id = a.tenant_id WHERE e.actor_type = 'account'"
    ))
    op.execute(sa.text(
        "UPDATE outbox_command o JOIN runner_v1 x ON x.id = o.aggregate_id "
        "JOIN run r ON r.id = x.run_id SET o.tenant_id = r.tenant_id, "
        "o.account_id = r.account_id WHERE o.topic = 'runtime.runner'"
    ))
    op.execute(sa.text(
        "UPDATE outbox_command o JOIN tool_invocation i ON i.id = o.aggregate_id "
        "SET o.tenant_id = i.tenant_id, o.account_id = i.account_id "
        "WHERE o.topic = 'runtime.sandbox'"
    ))
    op.execute(sa.text(
        "UPDATE outbox_command o JOIN run r ON r.id = o.aggregate_id "
        "SET o.tenant_id = r.tenant_id, o.account_id = r.account_id "
        "WHERE o.topic IN ('runtime.control','runtime.maintenance') "
        "AND o.aggregate_type = 'run'"
    ))
    op.execute(sa.text(
        "UPDATE outbox_command o JOIN group_run g ON g.id = o.aggregate_id "
        "JOIN run r ON r.id = g.run_id SET o.tenant_id = r.tenant_id, "
        "o.account_id = r.account_id WHERE o.topic = 'runtime.control' "
        "AND o.aggregate_type = 'group_run'"
    ))
    op.execute(sa.text(
        "UPDATE outbox_command o JOIN history_summary h ON h.id = o.aggregate_id "
        "JOIN user_session s ON s.id = h.session_id SET o.tenant_id = s.tenant_id, "
        "o.account_id = s.account_id WHERE o.topic = 'runtime.maintenance' "
        "AND o.aggregate_type = 'history_summary'"
    ))
    op.execute(sa.text(
        "UPDATE outbox_command o JOIN notification_delivery d ON d.id = o.aggregate_id "
        "JOIN user_session s ON s.id = d.session_id SET o.tenant_id = s.tenant_id, "
        "o.account_id = s.account_id WHERE o.topic = 'runtime.delivery'"
    ))

    _restore_backfill_update_guards()

    op.alter_column("user_session", "account_id", existing_type=sa.String(36), nullable=False)
    for table in TENANT_COLUMNS:
        _enforce_tenant_column(table)
    for table in ("run", "tool_invocation", "agent_invocation"):
        op.alter_column(table, "account_id", existing_type=sa.String(36), nullable=False)
        _create_fk_if_missing(
            f"fk_{table}_account_v1", table, "platform_account", ["account_id"], ["id"]
        )
    _create_fk_if_missing(
        "fk_platform_audit_tenant_v1", "platform_audit_event", "tenant",
        ["tenant_id"], ["id"],
    )
    _create_fk_if_missing(
        "fk_outbox_tenant_v1", "outbox_command", "tenant", ["tenant_id"], ["id"]
    )
    _create_fk_if_missing(
        "fk_outbox_account_v1", "outbox_command", "platform_account",
        ["account_id"], ["id"],
    )

    for name, table in (
        ("uq_agent_id_tenant", "agent"),
        ("uq_user_session_id_tenant", "user_session"),
        ("uq_run_id_tenant", "run"),
        ("uq_work_item_id_tenant", "work_item"),
        ("uq_runner_id_tenant", "runner_v1"),
        ("uq_workspace_id_tenant", "workspace"),
        ("uq_memory_head_id_tenant", "memory_head"),
    ):
        _create_unique_if_missing(name, table, ["id", "tenant_id"])

    for name, child, parent, local_columns, remote_columns in TENANT_COMPOSITE_FKS:
        _create_fk_if_missing(
            name, child, parent, local_columns, remote_columns,
        )

    for table, (name, columns) in TENANT_INDEXES.items():
        _create_index_if_missing(name, table, columns)

    if not _has_table("tenant_membership"):
        op.create_table(
            "tenant_membership",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(36), nullable=False),
            sa.Column("account_id", sa.String(36), nullable=False),
            sa.Column("role_key", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("version", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("role_key IN ('member','admin')", name="ck_tenant_membership_role"),
            sa.CheckConstraint("status IN ('active','disabled')", name="ck_tenant_membership_status"),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
            sa.ForeignKeyConstraint(["account_id"], ["platform_account.id"]),
            sa.UniqueConstraint("tenant_id", "account_id", name="uq_tenant_membership_account"),
        )
    _create_index_if_missing(
        "ix_tenant_membership_account_status", "tenant_membership",
        ["account_id", "status", "tenant_id"],
    )
    op.execute(sa.text(
        "INSERT INTO tenant_membership "
        "(id, tenant_id, account_id, role_key, status, version, created_at, updated_at) "
        "SELECT UUID(), a.tenant_id, a.id, "
        "CASE WHEN EXISTS (SELECT 1 FROM platform_account_role r "
        "WHERE r.account_id = a.id AND r.role_key = 'admin') THEN 'admin' ELSE 'member' END, "
        "'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM platform_account a "
        "LEFT JOIN tenant_membership m ON m.tenant_id = a.tenant_id AND m.account_id = a.id "
        "WHERE m.id IS NULL"
    ))


def downgrade() -> None:
    op.drop_table("tenant_membership")

    for name, child, _, _, _ in reversed(TENANT_COMPOSITE_FKS):
        op.drop_constraint(name, child, type_="foreignkey")

    # MySQL may reuse one of our explicit Tenant indexes to enforce a scalar
    # foreign key.  Remove every dependent FK before removing those indexes.
    op.drop_constraint("fk_platform_audit_tenant_v1", "platform_audit_event", type_="foreignkey")
    op.drop_constraint("fk_outbox_account_v1", "outbox_command", type_="foreignkey")
    op.drop_constraint("fk_outbox_tenant_v1", "outbox_command", type_="foreignkey")
    for table in ("agent_invocation", "tool_invocation", "run"):
        op.drop_constraint(f"fk_{table}_account_v1", table, type_="foreignkey")
    for table in reversed(TENANT_COLUMNS):
        op.drop_constraint(_tenant_fk_name(table), table, type_="foreignkey")
    op.drop_constraint("fk_platform_account_tenant_v1", "platform_account", type_="foreignkey")

    for table, (name, _) in reversed(tuple(TENANT_INDEXES.items())):
        op.drop_index(name, table_name=table)
    for name, table in (
        ("uq_workspace_id_tenant", "workspace"),
        ("uq_runner_id_tenant", "runner_v1"),
        ("uq_work_item_id_tenant", "work_item"),
        ("uq_run_id_tenant", "run"),
        ("uq_user_session_id_tenant", "user_session"),
        ("uq_agent_id_tenant", "agent"),
        ("uq_memory_head_id_tenant", "memory_head"),
    ):
        op.drop_constraint(name, table, type_="unique")

    op.drop_column("platform_audit_event", "tenant_id")
    op.drop_column("outbox_command", "account_id")
    op.drop_column("outbox_command", "tenant_id")
    for table in ("agent_invocation", "tool_invocation", "run"):
        op.drop_column(table, "account_id")
    for table in reversed(TENANT_COLUMNS):
        op.drop_column(table, "tenant_id")
    op.drop_constraint("uq_platform_account_id_tenant", "platform_account", type_="unique")
    op.drop_column("platform_account", "tenant_id")
    op.drop_index("ix_tenant_status_created", table_name="tenant")
    op.drop_table("tenant")
