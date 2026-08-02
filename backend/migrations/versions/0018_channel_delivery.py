"""Channel bindings and NotificationDelivery state

Revision ID: 0018
Revises: 0017
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"; down_revision = "0017"; branch_labels = None; depends_on = None

def timestamps(): return [
    sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
]

def upgrade():
    op.create_table("channel_account",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("plugin_key", sa.String(191), nullable=False),
        sa.Column("owner_account_id", sa.String(36), sa.ForeignKey("platform_account.id"), nullable=False),
        sa.Column("external_account_key", sa.String(500), nullable=False), sa.Column("credential_ref", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"), sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"), *timestamps(),
        sa.CheckConstraint("status IN ('active','disabled','revoked')", name="ck_channel_account_status"),
        sa.UniqueConstraint("plugin_key", "external_account_key", name="uq_channel_account_external"))
    op.create_foreign_key("fk_user_session_origin_channel", "user_session", "channel_account", ["origin_channel_account_id"], ["id"])
    op.create_table("agent_channel_binding",
        sa.Column("channel_account_id", sa.String(36), sa.ForeignKey("channel_account.id"), primary_key=True),
        sa.Column("external_route_key", sa.String(500), primary_key=True), sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"), sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"), *timestamps(),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_agent_channel_binding_status"),
        sa.UniqueConstraint("channel_account_id", "external_route_key", name="uq_agent_channel_route"))
    op.create_table("channel_identity_binding",
        sa.Column("channel_account_id", sa.String(36), sa.ForeignKey("channel_account.id"), primary_key=True),
        sa.Column("external_user_id_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False), sa.Column("verified_at", sa.DateTime(), nullable=False))
    op.create_table("channel_conversation_binding",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("channel_account_id", sa.String(36), sa.ForeignKey("channel_account.id"), nullable=False),
        sa.Column("external_conversation_id", sa.String(300), nullable=False), sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"), sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"), *timestamps(),
        sa.CheckConstraint("status IN ('active','closed')", name="ck_channel_conversation_binding_status"),
        sa.UniqueConstraint("channel_account_id", "external_conversation_id", name="uq_channel_conversation_external"),
        sa.UniqueConstraint("session_id", name="uq_channel_conversation_session"))
    op.create_table("notification_delivery",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("session_id", sa.String(36), sa.ForeignKey("user_session.id"), nullable=False),
        sa.Column("user_id", sa.String(191), sa.ForeignKey("user_identity.user_id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id")), sa.Column("group_run_id", sa.String(36), sa.ForeignKey("group_run.id")),
        sa.Column("triggering_event_id", sa.String(36), sa.ForeignKey("execution_event.id"), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False), sa.Column("template_key", sa.String(100), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False), sa.Column("channel_account_id", sa.String(36), sa.ForeignKey("channel_account.id"), nullable=False),
        sa.Column("external_conversation_id", sa.String(300), nullable=False), sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("dedupe_key", sa.String(191), nullable=False), sa.Column("provider_idempotency_key", sa.String(191), nullable=False),
        sa.Column("provider_message_key", sa.String(300)), sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(100)), sa.Column("last_error_detail", sa.Text()), sa.Column("next_attempt_at", sa.DateTime()),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"), sa.Column("delivered_at", sa.DateTime()), sa.Column("cancelled_at", sa.DateTime()), *timestamps(),
        sa.CheckConstraint("event_type IN ('clarification_required','graph_confirmation_required','tool_confirmation_required','run_completed','run_failed','run_cancelled')", name="ck_notification_delivery_event_type"),
        sa.CheckConstraint("status IN ('queued','delivering','retry_wait','succeeded','failed','cancelled','outcome_unknown')", name="ck_notification_delivery_status"),
        sa.UniqueConstraint("session_id", "dedupe_key", name="uq_notification_delivery_dedupe"),
        sa.UniqueConstraint("channel_account_id", "provider_idempotency_key", name="uq_notification_provider_idempotency"),
        sa.UniqueConstraint("channel_account_id", "external_conversation_id", "provider_message_key", name="uq_notification_provider_message"))
    op.create_index("ix_notification_delivery_claim", "notification_delivery", ["status", "next_attempt_at", "created_at"])
    op.create_index("ix_notification_delivery_session_created", "notification_delivery", ["session_id", "created_at"])
    op.create_index("ix_notification_delivery_run_status", "notification_delivery", ["run_id", "status"])
    op.create_table("notification_delivery_attempt",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("delivery_id", sa.String(36), sa.ForeignKey("notification_delivery.id"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False), sa.Column("status", sa.String(24), nullable=False), sa.Column("claim_token", sa.String(36), nullable=False),
        sa.Column("provider_request_key", sa.String(191)), sa.Column("provider_message_key", sa.String(300)),
        sa.Column("request_metadata", sa.JSON(), nullable=False), sa.Column("response_metadata", sa.JSON()),
        sa.Column("error_code", sa.String(100)), sa.Column("error_detail", sa.Text()), sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()), *timestamps(),
        sa.CheckConstraint("status IN ('queued','delivering','succeeded','failed','timed_out','outcome_unknown')", name="ck_notification_attempt_status"),
        sa.UniqueConstraint("delivery_id", "attempt_no", name="uq_notification_attempt_no"),
        sa.UniqueConstraint("delivery_id", "provider_request_key", name="uq_notification_attempt_provider_request"))
    op.execute(sa.text("""
        CREATE TRIGGER trg_memory_head_logical_key_frozen BEFORE UPDATE ON memory_head FOR EACH ROW
        BEGIN
          IF NOT (NEW.id <=> OLD.id AND NEW.user_id <=> OLD.user_id AND NEW.scope <=> OLD.scope
            AND NEW.agent_id <=> OLD.agent_id AND NEW.fact_key <=> OLD.fact_key AND NEW.created_at <=> OLD.created_at) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MemoryHead logical key is immutable';
          END IF;
        END
    """))

def downgrade():
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_memory_head_logical_key_frozen"))
    op.drop_table("notification_delivery_attempt"); op.drop_table("notification_delivery")
    op.drop_table("channel_conversation_binding"); op.drop_table("channel_identity_binding"); op.drop_table("agent_channel_binding")
    op.drop_constraint("fk_user_session_origin_channel", "user_session", type_="foreignkey"); op.drop_table("channel_account")
