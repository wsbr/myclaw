"""Add Tenant-scoped local document knowledge and immutable indexes.

Revision ID: 0039
Revises: 0038
"""

from alembic import op
import sqlalchemy as sa


revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_base",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("owner_account_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("sharing_policy", sa.String(24), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("current_index_revision_id", sa.String(36), nullable=True),
        sa.Column("next_revision_no", sa.BigInteger(), nullable=False),
        sa.Column("chunking_profile", sa.JSON(), nullable=False),
        sa.Column("embedding_profile", sa.JSON(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "sharing_policy IN ('owner_only','selected_agents')",
            name="ck_knowledge_base_sharing",
        ),
        sa.CheckConstraint(
            "status IN ('active','deleted')", name="ck_knowledge_base_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["owner_account_id", "tenant_id"],
            ["platform_account.id", "platform_account.tenant_id"],
            name="fk_knowledge_base_owner_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id", "owner_account_id", "name",
            name="uq_knowledge_base_owner_name",
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_knowledge_base_id_tenant"),
    )
    op.create_index(
        "ix_knowledge_base_tenant_owner_status", "knowledge_base",
        ["tenant_id", "owner_account_id", "status", "updated_at"],
    )

    op.create_table(
        "knowledge_base_agent_access",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("granted_by_account_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id", "tenant_id"],
            ["knowledge_base.id", "knowledge_base.tenant_id"],
            name="fk_knowledge_access_base_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id", "tenant_id"], ["agent.id", "agent.tenant_id"],
            name="fk_knowledge_access_agent_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_account_id", "tenant_id"],
            ["platform_account.id", "platform_account.tenant_id"],
            name="fk_knowledge_access_grantor_tenant",
        ),
        sa.UniqueConstraint(
            "knowledge_base_id", "agent_id", name="uq_knowledge_base_agent_access",
        ),
    )
    op.create_index(
        "ix_knowledge_agent_access_tenant_agent", "knowledge_base_agent_access",
        ["tenant_id", "agent_id", "knowledge_base_id"],
    )

    op.create_table(
        "knowledge_document",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(36), nullable=False),
        sa.Column("logical_key", sa.String(500), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("current_source_id", sa.String(36), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','deleted')", name="ck_knowledge_document_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id", "tenant_id"],
            ["knowledge_base.id", "knowledge_base.tenant_id"],
            name="fk_knowledge_document_base_tenant",
        ),
        sa.UniqueConstraint(
            "knowledge_base_id", "logical_key", name="uq_knowledge_document_logical_key",
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_knowledge_document_id_tenant"),
    )
    op.create_index(
        "ix_knowledge_document_tenant_base_status", "knowledge_document",
        ["tenant_id", "knowledge_base_id", "status", "updated_at"],
    )

    op.create_table(
        "knowledge_source",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(36), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("parse_metadata", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_by_account_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('indexed','superseded','deleted','failed')",
            name="ck_knowledge_source_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id", "tenant_id"],
            ["knowledge_base.id", "knowledge_base.tenant_id"],
            name="fk_knowledge_source_base_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["knowledge_document.id", "knowledge_document.tenant_id"],
            name="fk_knowledge_source_document_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_account_id", "tenant_id"],
            ["platform_account.id", "platform_account.tenant_id"],
            name="fk_knowledge_source_creator_tenant",
        ),
        sa.UniqueConstraint(
            "knowledge_base_id", "checksum", name="uq_knowledge_source_checksum",
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_knowledge_source_id_tenant"),
        sa.UniqueConstraint("storage_key", name="uq_knowledge_source_storage_key"),
    )
    op.create_index(
        "ix_knowledge_source_tenant_document_created", "knowledge_source",
        ["tenant_id", "document_id", "created_at"],
    )
    op.create_foreign_key(
        "fk_knowledge_document_current_source", "knowledge_document",
        "knowledge_source", ["current_source_id"], ["id"],
    )

    op.create_table(
        "knowledge_index_revision",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(36), nullable=False),
        sa.Column("revision_no", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source_manifest", sa.JSON(), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=False),
        sa.Column("chunking_profile", sa.JSON(), nullable=False),
        sa.Column("embedding_profile", sa.JSON(), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_by_account_id", sa.String(36), nullable=False),
        sa.Column("built_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ready','failed')", name="ck_knowledge_index_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id", "tenant_id"],
            ["knowledge_base.id", "knowledge_base.tenant_id"],
            name="fk_knowledge_index_base_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_account_id", "tenant_id"],
            ["platform_account.id", "platform_account.tenant_id"],
            name="fk_knowledge_index_creator_tenant",
        ),
        sa.UniqueConstraint(
            "knowledge_base_id", "revision_no", name="uq_knowledge_index_revision_no",
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_knowledge_index_id_tenant"),
        sa.UniqueConstraint(
            "knowledge_base_id", "content_hash",
            name="uq_knowledge_index_content_hash",
        ),
    )
    op.create_index(
        "ix_knowledge_index_tenant_base_revision", "knowledge_index_revision",
        ["tenant_id", "knowledge_base_id", "revision_no"],
    )
    op.create_foreign_key(
        "fk_knowledge_base_current_index", "knowledge_base",
        "knowledge_index_revision", ["current_index_revision_id"], ["id"],
    )

    op.create_table(
        "knowledge_chunk",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("index_revision_id", sa.String(36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(36), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(500), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("term_frequencies", sa.JSON(), nullable=False),
        sa.Column("vector", sa.JSON(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["index_revision_id", "tenant_id"],
            ["knowledge_index_revision.id", "knowledge_index_revision.tenant_id"],
            name="fk_knowledge_chunk_index_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id", "tenant_id"],
            ["knowledge_base.id", "knowledge_base.tenant_id"],
            name="fk_knowledge_chunk_base_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["knowledge_document.id", "knowledge_document.tenant_id"],
            name="fk_knowledge_chunk_document_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "tenant_id"],
            ["knowledge_source.id", "knowledge_source.tenant_id"],
            name="fk_knowledge_chunk_source_tenant",
        ),
        sa.UniqueConstraint(
            "index_revision_id", "source_id", "ordinal",
            name="uq_knowledge_chunk_source_ordinal",
        ),
    )
    op.create_index(
        "ix_knowledge_chunk_tenant_revision", "knowledge_chunk",
        ["tenant_id", "index_revision_id", "ordinal"],
    )

    op.create_table(
        "knowledge_retrieval_snapshot",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("runner_id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("revision_manifest", sa.JSON(), nullable=False),
        sa.Column("revision_manifest_hash", sa.String(64), nullable=False),
        sa.Column("result_snapshot", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["account_id", "tenant_id"],
            ["platform_account.id", "platform_account.tenant_id"],
            name="fk_knowledge_snapshot_account_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["runner_id", "tenant_id"],
            ["runner_v1.id", "runner_v1.tenant_id"],
            name="fk_knowledge_snapshot_runner_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id", "tenant_id"], ["agent.id", "agent.tenant_id"],
            name="fk_knowledge_snapshot_agent_tenant",
        ),
        sa.UniqueConstraint(
            "runner_id", "query_hash", "revision_manifest_hash",
            name="uq_knowledge_retrieval_runner_query",
        ),
    )
    op.create_index(
        "ix_knowledge_retrieval_tenant_runner", "knowledge_retrieval_snapshot",
        ["tenant_id", "runner_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("knowledge_retrieval_snapshot")
    op.drop_table("knowledge_chunk")
    op.drop_constraint(
        "fk_knowledge_base_current_index", "knowledge_base", type_="foreignkey",
    )
    op.drop_table("knowledge_index_revision")
    op.drop_constraint(
        "fk_knowledge_document_current_source", "knowledge_document",
        type_="foreignkey",
    )
    op.drop_table("knowledge_source")
    op.drop_table("knowledge_document")
    op.drop_table("knowledge_base_agent_access")
    op.drop_table("knowledge_base")
