"""phase-one identity, registry, event, audit, and outbox foundation

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _timestamps(*, updated: bool = True) -> list[sa.Column]:
    columns = [
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"))
    ]
    if updated:
        columns.append(
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
    return columns


def upgrade() -> None:
    op.create_table(
        "user_identity",
        sa.Column("user_id", sa.String(191), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        *_timestamps(updated=False),
        sa.CheckConstraint("status IN ('active','orphaned')", name="ck_user_identity_status"),
    )
    op.create_table(
        "platform_account",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False, unique=True),
        sa.Column("email", sa.String(255), unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("auth_revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('active','discarded','disabled')", name="ck_platform_account_status"
        ),
    )
    op.create_index("ix_platform_account_status_created", "platform_account", ["status", "created_at"])
    op.create_table(
        "platform_account_role",
        sa.Column("account_id", sa.String(36), sa.ForeignKey("platform_account.id"), primary_key=True),
        sa.Column("role_key", sa.String(20), primary_key=True),
        sa.Column("granted_by_account_id", sa.String(36), sa.ForeignKey("platform_account.id")),
        sa.Column("granted_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("role_key IN ('user','builder','admin')", name="ck_account_role_key"),
    )
    op.create_table(
        "idempotency_record",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("principal_type", sa.String(32), nullable=False),
        sa.Column("principal_id", sa.String(191), nullable=False),
        sa.Column("operation", sa.String(100), nullable=False),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64)),
        sa.Column("resource_id", sa.String(36)),
        sa.Column("http_status", sa.Integer()),
        sa.Column("response_snapshot", sa.JSON()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        *_timestamps(updated=False),
        sa.UniqueConstraint(
            "principal_type", "principal_id", "operation", "idempotency_key",
            name="uq_idempotency_principal_operation_key",
        ),
    )
    op.create_index("ix_idempotency_expiry", "idempotency_record", ["expires_at"])

    op.create_table(
        "model",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(191), nullable=False, unique=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("endpoint", sa.String(500)),
        sa.Column("credential_ref", sa.String(500)),
        sa.Column("default_parameters", sa.JSON(), nullable=False),
        sa.Column("parameter_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_by_account_id", sa.String(36), sa.ForeignKey("platform_account.id")),
        *_timestamps(),
    )
    op.create_index("ix_model_provider_enabled", "model", ["provider", "enabled"])
    op.create_table(
        "platform_tool",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tool_key", sa.String(191), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("output_schema", sa.JSON()),
        sa.Column("implementation_ref", sa.String(500), nullable=False),
        sa.Column("implementation_version", sa.String(64), nullable=False),
        sa.Column("execution_mode", sa.String(20), nullable=False, server_default="inline"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("confirmation_policy", sa.String(20), nullable=False, server_default="never"),
        sa.Column("platform_config", sa.JSON(), nullable=False),
        sa.Column("config_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("registration_hash", sa.String(64), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("execution_mode IN ('inline','durable_async')", name="ck_platform_tool_mode"),
        sa.CheckConstraint("confirmation_policy IN ('never','always')", name="ck_platform_tool_confirmation"),
    )
    op.create_index("ix_platform_tool_enabled_key", "platform_tool", ["enabled", "tool_key"])
    op.create_table(
        "skill",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("logical_name", sa.String(191), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("owner_account_id", sa.String(36), sa.ForeignKey("platform_account.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("current_published_version_id", sa.String(36)),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("status IN ('active','inactive')", name="ck_skill_status"),
    )
    op.create_table(
        "skill_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("skill.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="uploaded"),
        sa.Column("package_uri", sa.String(1000), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("manifest_name", sa.String(200), nullable=False),
        sa.Column("manifest_description", sa.Text(), nullable=False),
        sa.Column("validation_error", sa.Text()),
        sa.Column("created_by_account_id", sa.String(36), sa.ForeignKey("platform_account.id"), nullable=False),
        sa.Column("published_at", sa.DateTime()),
        *_timestamps(updated=False),
        sa.UniqueConstraint("skill_id", "version_no", name="uq_skill_version_number"),
        sa.UniqueConstraint("skill_id", "checksum", name="uq_skill_version_checksum"),
        sa.CheckConstraint(
            "status IN ('uploaded','validating','published','rejected','deprecated')",
            name="ck_skill_version_status",
        ),
    )
    op.create_index("ix_skill_version_status", "skill_version", ["skill_id", "status", "version_no"])
    op.create_table(
        "policy_definition",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("policy_key", sa.String(191), nullable=False, unique=True),
        sa.Column("policy_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("implementation_ref", sa.String(500), nullable=False),
        sa.Column("implementation_version", sa.String(64), nullable=False),
        sa.Column("default_parameters", sa.JSON(), nullable=False),
        sa.Column("parameter_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.CheckConstraint(
            "policy_type IN ('context','history','memory_write','memory_recall')",
            name="ck_policy_definition_type",
        ),
    )
    op.create_index("ix_policy_definition_type_enabled", "policy_definition", ["policy_type", "enabled"])
    op.create_table(
        "sandbox_profile",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("profile_key", sa.String(191), nullable=False, unique=True),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("implementation_ref", sa.String(500), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("config_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )

    op.create_table(
        "user_session",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("entry_agent_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("title", sa.String(500)),
        sa.Column("origin_type", sa.String(20), nullable=False, server_default="web"),
        sa.Column("origin_api_key_id", sa.String(36)),
        sa.Column("origin_channel_account_id", sa.String(36)),
        sa.Column("origin_external_conversation_id", sa.String(500)),
        sa.Column("next_event_sequence", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("closed_at", sa.DateTime()),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.UniqueConstraint("id", "user_id", name="uq_user_session_id_user"),
        sa.CheckConstraint("status IN ('active','closed','archived')", name="ck_user_session_status"),
        sa.CheckConstraint("origin_type IN ('web','api','channel')", name="ck_user_session_origin"),
    )
    op.create_index("ix_user_session_owner_status", "user_session", ["user_id", "status", "updated_at"])
    op.create_table(
        "execution_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("run_id", sa.String(36)),
        sa.Column("group_session_id", sa.String(36)),
        sa.Column("group_run_id", sa.String(36)),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("event_dedupe_key", sa.String(191), nullable=False),
        sa.Column("transition_key", sa.String(191)),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(191)),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("entity_version", sa.BigInteger()),
        sa.Column("visibility", sa.String(20), nullable=False),
        sa.Column("private_owner_type", sa.String(32)),
        sa.Column("private_owner_id", sa.String(36)),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("caused_by_event_id", sa.String(36), sa.ForeignKey("execution_event.id")),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        *_timestamps(updated=False),
        sa.UniqueConstraint("session_id", "sequence", name="uq_execution_event_sequence"),
        sa.UniqueConstraint("session_id", "event_dedupe_key", name="uq_execution_event_dedupe"),
        sa.CheckConstraint("visibility IN ('user','group','direct','private')", name="ck_execution_event_visibility"),
        sa.CheckConstraint(
            "visibility != 'private' OR (private_owner_type IS NOT NULL AND private_owner_id IS NOT NULL)",
            name="ck_private_event_owner",
        ),
    )
    op.create_index("ix_execution_event_session_scan", "execution_event", ["session_id", "sequence"])
    op.create_index("ix_execution_event_entity", "execution_event", ["entity_type", "entity_id", "created_at"])
    op.create_index("ix_execution_event_transition", "execution_event", ["transition_key"])
    op.create_table(
        "execution_event_recipient",
        sa.Column("event_id", sa.String(36), sa.ForeignKey("execution_event.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("recipient_type", sa.String(32), primary_key=True),
        sa.Column("recipient_id", sa.String(191), primary_key=True),
        sa.CheckConstraint(
            "recipient_type IN ('user_identity','group_node','agent')",
            name="ck_execution_event_recipient_type",
        ),
    )
    op.create_index(
        "ix_execution_event_recipient_lookup", "execution_event_recipient",
        ["recipient_type", "recipient_id", "event_id"],
    )
    op.create_table(
        "platform_audit_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(191)),
        sa.Column("actor_user_id", sa.String(191)),
        sa.Column("request_id", sa.String(64)),
        sa.Column("correlation_id", sa.String(64)),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(191), nullable=False),
        sa.Column("resource_version", sa.BigInteger()),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False, server_default="info"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("changes", sa.JSON()),
        sa.Column("metadata", sa.JSON()),
        sa.Column("source_ip_hash", sa.String(64)),
        sa.Column("user_agent_hash", sa.String(64)),
        sa.Column("dedupe_key", sa.String(191), nullable=False, unique=True),
        *_timestamps(updated=False),
        sa.CheckConstraint("outcome IN ('succeeded','failed')", name="ck_platform_audit_outcome"),
        sa.CheckConstraint("severity IN ('info','warning','critical')", name="ck_platform_audit_severity"),
    )
    op.create_index("ix_platform_audit_resource", "platform_audit_event", ["resource_type", "resource_id", "occurred_at"])
    op.create_index("ix_platform_audit_actor", "platform_audit_event", ["actor_type", "actor_id", "occurred_at"])
    op.create_index("ix_platform_audit_filter", "platform_audit_event", ["category", "action", "occurred_at"])
    op.create_table(
        "outbox_command",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("topic", sa.String(32), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(191), nullable=False),
        sa.Column("partition_key", sa.String(191), nullable=False),
        sa.Column("dedupe_key", sa.String(191), nullable=False),
        sa.Column("payload_ref", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatch_lease_owner", sa.String(191)),
        sa.Column("dispatch_lease_token", sa.String(36)),
        sa.Column("dispatch_lease_expires_at", sa.DateTime()),
        sa.Column("last_error", sa.Text()),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        *_timestamps(),
        sa.UniqueConstraint("topic", "dedupe_key", name="uq_outbox_topic_dedupe"),
        sa.CheckConstraint(
            "topic IN ('runtime.control','runtime.runner','runtime.tool','runtime.sandbox','runtime.delivery','runtime.maintenance')",
            name="ck_outbox_topic",
        ),
        sa.CheckConstraint(
            "status IN ('pending','claimed','retry_wait','completed','dead')",
            name="ck_outbox_status",
        ),
    )
    op.create_index(
        "ix_outbox_claim", "outbox_command",
        ["topic", "status", "available_at", "priority", "created_at"],
    )
    op.create_index("ix_outbox_reclaim", "outbox_command", ["status", "dispatch_lease_expires_at"])


def downgrade() -> None:
    for table in (
        "outbox_command",
        "platform_audit_event",
        "execution_event_recipient",
        "execution_event",
        "user_session",
        "sandbox_profile",
        "policy_definition",
        "skill_version",
        "skill",
        "platform_tool",
        "model",
        "idempotency_record",
        "platform_account_role",
        "platform_account",
        "user_identity",
    ):
        op.drop_table(table)
