"""Add semantic Embedding and Qdrant projection metadata.

Revision ID: 0041
Revises: 0040
"""

from alembic import op
import sqlalchemy as sa


revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_outbox_topic", "outbox_command", type_="check")
    op.create_check_constraint(
        "ck_outbox_topic", "outbox_command",
        "topic IN ('runtime.control','runtime.runner','runtime.tool','runtime.sandbox',"
        "'runtime.delivery','runtime.maintenance','knowledge.index')",
    )

    op.add_column(
        "knowledge_base", sa.Column("pending_index_revision_id", sa.String(36)),
    )
    op.add_column(
        "knowledge_base", sa.Column("embedding_model_id", sa.String(36)),
    )
    op.create_foreign_key(
        "fk_knowledge_base_pending_index", "knowledge_base",
        "knowledge_index_revision", ["pending_index_revision_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_knowledge_base_embedding_model", "knowledge_base",
        "model", ["embedding_model_id"], ["id"],
    )

    op.drop_constraint("ck_knowledge_index_status", "knowledge_index_revision", type_="check")
    op.create_check_constraint(
        "ck_knowledge_index_status", "knowledge_index_revision",
        "status IN ('queued','running','ready','failed')",
    )
    op.add_column(
        "knowledge_index_revision",
        sa.Column("embedding_model_version_id", sa.String(36)),
    )
    op.add_column(
        "knowledge_index_revision",
        sa.Column("vector_backend", sa.String(32), nullable=False, server_default="mysql_json"),
    )
    op.add_column(
        "knowledge_index_revision", sa.Column("vector_collection", sa.String(191)),
    )
    op.add_column(
        "knowledge_index_revision",
        sa.Column("vector_point_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "knowledge_index_revision", sa.Column("started_at", sa.DateTime()),
    )
    op.add_column(
        "knowledge_index_revision", sa.Column("completed_at", sa.DateTime()),
    )
    op.create_foreign_key(
        "fk_knowledge_index_embedding_model_version", "knowledge_index_revision",
        "model_version", ["embedding_model_version_id"], ["id"],
    )
    op.add_column(
        "knowledge_chunk", sa.Column("embedding_checksum", sa.String(64)),
    )
    op.create_index(
        "ix_knowledge_index_embedding_status", "knowledge_index_revision",
        ["embedding_model_version_id", "status", "built_at"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_knowledge_index_embedding_model_version", "knowledge_index_revision",
        type_="foreignkey",
    )
    op.drop_index("ix_knowledge_index_embedding_status", table_name="knowledge_index_revision")
    op.drop_column("knowledge_chunk", "embedding_checksum")
    for column in (
        "completed_at", "started_at", "vector_point_count", "vector_collection",
        "vector_backend", "embedding_model_version_id",
    ):
        op.drop_column("knowledge_index_revision", column)
    op.drop_constraint("ck_knowledge_index_status", "knowledge_index_revision", type_="check")
    op.create_check_constraint(
        "ck_knowledge_index_status", "knowledge_index_revision",
        "status IN ('ready','failed')",
    )
    op.drop_constraint(
        "fk_knowledge_base_embedding_model", "knowledge_base", type_="foreignkey",
    )
    op.drop_constraint(
        "fk_knowledge_base_pending_index", "knowledge_base", type_="foreignkey",
    )
    op.drop_column("knowledge_base", "embedding_model_id")
    op.drop_column("knowledge_base", "pending_index_revision_id")

    op.drop_constraint("ck_outbox_topic", "outbox_command", type_="check")
    op.create_check_constraint(
        "ck_outbox_topic", "outbox_command",
        "topic IN ('runtime.control','runtime.runner','runtime.tool','runtime.sandbox',"
        "'runtime.delivery','runtime.maintenance')",
    )
