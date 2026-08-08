"""provider-neutral immutable Tool contracts and connections

Revision ID: 0030
Revises: 0029

This is intentionally a breaking schema revision. Deployments applying it are
required to start from the documented empty MyClaw database; no legacy Tool or
Agent data is migrated.
"""

from alembic import op
import sqlalchemy as sa


revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    ]


def upgrade() -> None:
    # MySQL DDL is not transactionally rolled back. Refuse before the first
    # schema mutation if any legacy business data exists, so this migration
    # cannot accidentally become an in-place conversion path.
    bind = op.get_bind()
    legacy_tables = (
        "platform_account", "model", "platform_tool", "agent_blueprint",
        "user_session", "memory_record", "artifact", "tool_invocation",
    )
    populated = []
    inspector = sa.inspect(bind)
    available = set(inspector.get_table_names())
    for table_name in legacy_tables:
        if table_name not in available:
            continue
        count = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM `{table_name}`")
        ).scalar_one()
        if count:
            populated.append(f"{table_name}={count}")
    if populated:
        raise RuntimeError(
            "0030 is reset-only; stop MyClaw and erase the project MySQL, "
            "Redis and Workspace/Artifact/Sandbox volumes before upgrading "
            f"({', '.join(populated)})"
        )

    op.create_table(
        "platform_secret",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("purpose", sa.String(100), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("rotated_from_id", sa.String(36), sa.ForeignKey("platform_secret.id")),
        sa.Column("created_by_account_id", sa.String(36), sa.ForeignKey("platform_account.id")),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("status IN ('active','rotated','revoked')", name="ck_platform_secret_status"),
    )
    op.create_index("ix_platform_secret_purpose_status", "platform_secret", ["purpose", "status"])

    op.create_table(
        "tool_connection",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_key", sa.String(191), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("connection_type", sa.String(32), nullable=False),
        sa.Column("auth_type", sa.String(20), nullable=False, server_default="none"),
        sa.Column("status", sa.String(20), nullable=False, server_default="inactive"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "primary_search_slot", sa.Integer(),
            sa.Computed("CASE WHEN is_primary = 1 THEN 1 ELSE NULL END", persisted=True),
        ),
        sa.Column("current_revision_id", sa.String(36)),
        sa.Column("next_revision_no", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("health_status", sa.String(32), nullable=False, server_default="untested"),
        sa.Column("health_checked_at", sa.DateTime()),
        sa.Column("health_latency_ms", sa.Integer()),
        sa.Column("health_error_code", sa.String(100)),
        sa.Column("created_by_account_id", sa.String(36), sa.ForeignKey("platform_account.id")),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("connection_type IN ('bocha','brave','mcp','browser')", name="ck_tool_connection_type"),
        sa.CheckConstraint("auth_type IN ('none','bearer','x_api_key')", name="ck_tool_connection_auth"),
        sa.CheckConstraint("status IN ('active','inactive')", name="ck_tool_connection_status"),
    )
    op.create_index("ix_tool_connection_type_primary", "tool_connection", ["connection_type", "is_primary"])
    op.create_index(
        "uq_tool_connection_single_primary", "tool_connection",
        ["primary_search_slot"], unique=True,
    )
    op.create_table(
        "tool_connection_revision",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("tool_connection.id"), nullable=False),
        sa.Column("revision_no", sa.BigInteger(), nullable=False),
        sa.Column("endpoint", sa.String(1000), nullable=False),
        sa.Column("auth_type", sa.String(20), nullable=False),
        sa.Column("secret_id", sa.String(36), sa.ForeignKey("platform_secret.id")),
        sa.Column("secret_fingerprint", sa.String(64)),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("created_by_account_id", sa.String(36), sa.ForeignKey("platform_account.id")),
        sa.UniqueConstraint("connection_id", "revision_no", name="uq_tool_connection_revision_no"),
        sa.UniqueConstraint("connection_id", "content_hash", name="uq_tool_connection_revision_hash"),
    )
    op.create_foreign_key(
        "fk_tool_connection_current_revision", "tool_connection", "tool_connection_revision",
        ["current_revision_id"], ["id"],
    )

    op.drop_index("ix_platform_tool_enabled_key", table_name="platform_tool")
    op.drop_constraint("ck_platform_tool_mode", "platform_tool", type_="check")
    op.drop_constraint("ck_platform_tool_confirmation", "platform_tool", type_="check")
    op.drop_constraint("ck_platform_tool_side_effect", "platform_tool", type_="check")
    for column in (
        sa.Column("namespace", sa.String(100), nullable=False, server_default="platform"),
        sa.Column("origin", sa.String(20), nullable=False, server_default="builtin"),
        sa.Column("display_name_zh", sa.String(200), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("current_version_id", sa.String(36)),
        sa.Column("next_version_no", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
    ):
        op.add_column("platform_tool", column)
    for name in (
        "description", "input_schema", "output_schema", "implementation_ref",
        "implementation_version", "execution_mode", "side_effect", "supports_cancel",
        "supports_status_lookup", "enabled", "confirmation_policy", "platform_config",
        "config_version", "registration_hash",
    ):
        op.drop_column("platform_tool", name)
    op.create_check_constraint(
        "ck_tool_definition_origin", "platform_tool", "origin IN ('builtin','runtime','mcp')"
    )
    op.create_check_constraint(
        "ck_tool_definition_status", "platform_tool", "status IN ('active','inactive')"
    )
    op.create_unique_constraint(
        "uq_tool_definition_namespace_key", "platform_tool", ["namespace", "tool_key"]
    )
    op.create_index("ix_tool_definition_status_key", "platform_tool", ["status", "tool_key"])

    op.create_table(
        "tool_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("definition_id", sa.String(36), sa.ForeignKey("platform_tool.id"), nullable=False),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("model_description", sa.Text(), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=False),
        sa.Column("examples", sa.JSON(), nullable=False),
        sa.Column("error_catalog", sa.JSON(), nullable=False),
        sa.Column("annotations", sa.JSON(), nullable=False),
        sa.Column("implementation_ref", sa.String(1000), nullable=False),
        sa.Column("implementation_version", sa.String(64), nullable=False),
        sa.Column("execution_mode", sa.String(20), nullable=False),
        sa.Column("side_effect", sa.String(20), nullable=False),
        sa.Column("supports_cancel", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("supports_status_lookup", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("default_connection_id", sa.String(36), sa.ForeignKey("tool_connection.id")),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("execution_mode IN ('inline','durable_async')", name="ck_tool_version_mode"),
        sa.CheckConstraint("side_effect IN ('none','idempotent','non_idempotent')", name="ck_tool_version_side_effect"),
        sa.UniqueConstraint("definition_id", "version_no", name="uq_tool_version_number"),
        sa.UniqueConstraint("definition_id", "content_hash", name="uq_tool_version_hash"),
    )
    op.create_index("ix_tool_version_definition_published", "tool_version", ["definition_id", "published_at"])
    op.create_foreign_key(
        "fk_tool_definition_current_version", "platform_tool", "tool_version",
        ["current_version_id"], ["id"],
    )
    op.create_table(
        "tool_governance",
        sa.Column("tool_definition_id", sa.String(36), sa.ForeignKey("platform_tool.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("confirmation_policy", sa.String(20), nullable=False, server_default="never"),
        sa.Column("network_policy", sa.JSON(), nullable=False),
        sa.Column("workspace_policy", sa.JSON(), nullable=False),
        sa.Column("risk_rules", sa.JSON(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("confirmation_policy IN ('never','per_call','session_grant')", name="ck_tool_governance_confirmation"),
    )

    op.drop_column("agent_version_tool", "tool_registration_hash")
    for column in (
        sa.Column("tool_version_id", sa.String(36), sa.ForeignKey("tool_version.id")),
        sa.Column("tool_version_content_hash", sa.String(64)),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("tool_connection.id")),
        sa.Column("connection_revision_id", sa.String(36), sa.ForeignKey("tool_connection_revision.id")),
        sa.Column("secret_fingerprint", sa.String(64)),
    ):
        op.add_column("agent_version_tool", column)

    op.drop_column("tool_invocation", "tool_registration_hash")
    for column in (
        sa.Column("tool_version_id", sa.String(36), sa.ForeignKey("tool_version.id"), nullable=False),
        sa.Column("tool_version_content_hash", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("tool_connection.id")),
        sa.Column("connection_revision_id", sa.String(36), sa.ForeignKey("tool_connection_revision.id")),
        sa.Column("secret_fingerprint", sa.String(64)),
    ):
        op.add_column("tool_invocation", column)

    op.drop_column("runner_tool_snapshot", "implementation_version")
    op.drop_column("runner_tool_snapshot", "config_version")
    for column in (
        sa.Column("tool_version_id", sa.String(36), sa.ForeignKey("tool_version.id"), nullable=False),
        sa.Column("tool_version_content_hash", sa.String(64), nullable=False),
        sa.Column("connection_revision_id", sa.String(36), sa.ForeignKey("tool_connection_revision.id")),
        sa.Column("secret_fingerprint", sa.String(64)),
    ):
        op.add_column("runner_tool_snapshot", column)

    op.add_column("capability_snapshot", sa.Column("authorized_tool_versions", sa.JSON(), nullable=False))
    op.add_column("agent_turn", sa.Column("presented_schema_bundle_hash", sa.String(64)))
    op.add_column("runner_checkpoint", sa.Column("loaded_tool_version_ids", sa.JSON(), nullable=False))
    op.add_column("runner_checkpoint", sa.Column("presented_schema_bundle_hash", sa.String(64)))

    op.create_table(
        "browser_session_state",
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspace.id")),
        sa.Column("worker_instance_id", sa.String(191)),
        sa.Column("status", sa.String(32), nullable=False, server_default="new"),
        sa.Column("page_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("current_url", sa.String(2000)),
        sa.Column("current_origin", sa.String(500)),
        sa.Column("dom_fingerprint", sa.String(64)),
        sa.Column("last_operation", sa.JSON(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("status IN ('new','ready','closed','failed','outcome_unknown')", name="ck_browser_session_status"),
    )
    op.create_index("ix_browser_session_status_updated", "browser_session_state", ["status", "updated_at"])

    # Draft selections intentionally point only at ToolDefinition rows. The
    # transition that seals a draft/test/published AgentVersion is permitted
    # only after every selected Tool has a complete, internally consistent
    # immutable contract and connection snapshot.
    op.execute(sa.text("""
        CREATE TRIGGER trg_agent_version_tool_snapshot_required
        BEFORE UPDATE ON agent_version
        FOR EACH ROW
        BEGIN
          IF OLD.immutable_at IS NULL AND NEW.immutable_at IS NOT NULL AND EXISTS (
            SELECT 1
              FROM agent_version_tool avt
              LEFT JOIN tool_version tv
                ON tv.id = avt.tool_version_id
               AND tv.definition_id = avt.platform_tool_id
               AND tv.content_hash = avt.tool_version_content_hash
              LEFT JOIN tool_connection_revision tcr
                ON tcr.id = avt.connection_revision_id
               AND tcr.connection_id = avt.connection_id
               AND tcr.secret_fingerprint <=> avt.secret_fingerprint
             WHERE avt.agent_version_id = NEW.id
               AND (
                 avt.tool_version_id IS NULL
                 OR avt.tool_version_content_hash IS NULL
                 OR tv.id IS NULL
                 OR (avt.connection_id IS NULL) <> (avt.connection_revision_id IS NULL)
                 OR (avt.connection_revision_id IS NOT NULL AND tcr.id IS NULL)
               )
          ) THEN
            SIGNAL SQLSTATE '45000'
              SET MESSAGE_TEXT='sealed AgentVersion requires exact ToolVersion and connection revision snapshots';
          END IF;
        END
    """))

    # MySQL enforces immutable contracts and frozen execution pointers even if
    # a future application path accidentally issues an UPDATE.
    for table_name in ("tool_version", "tool_connection_revision"):
        op.execute(sa.text(f"""
            CREATE TRIGGER trg_{table_name}_immutable_update
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='{table_name} is immutable'
        """))
        op.execute(sa.text(f"""
            CREATE TRIGGER trg_{table_name}_immutable_delete
            BEFORE DELETE ON {table_name}
            FOR EACH ROW
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='{table_name} is immutable'
        """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_platform_secret_material_immutable
        BEFORE UPDATE ON platform_secret
        FOR EACH ROW
        BEGIN
          IF NOT (OLD.purpose <=> NEW.purpose)
             OR NOT (OLD.ciphertext <=> NEW.ciphertext)
             OR NOT (OLD.fingerprint <=> NEW.fingerprint) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='platform secret material is immutable';
          END IF;
        END
    """))
    for table_name in ("agent_version_tool", "tool_invocation", "runner_tool_snapshot"):
        op.execute(sa.text(f"""
            CREATE TRIGGER trg_{table_name}_tool_binding_immutable
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW
            BEGIN
              IF NOT (OLD.tool_version_id <=> NEW.tool_version_id)
                 OR NOT (OLD.tool_version_content_hash <=> NEW.tool_version_content_hash)
                 OR NOT (OLD.connection_revision_id <=> NEW.connection_revision_id)
                 OR NOT (OLD.secret_fingerprint <=> NEW.secret_fingerprint) THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='{table_name} Tool binding is immutable';
              END IF;
            END
        """))


def downgrade() -> None:
    raise RuntimeError("0030 is a destructive reset-only migration and cannot be downgraded")
