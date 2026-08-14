"""Add immutable lexicons and versioned hybrid retrieval evidence.

Revision ID: 0042
Revises: 0041
"""

from alembic import op
import sqlalchemy as sa


revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_lexicon_revision",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(36), nullable=False),
        sa.Column("revision_no", sa.BigInteger(), nullable=False),
        sa.Column("terms", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_by_account_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_base.id"]),
        sa.ForeignKeyConstraint(
            ["created_by_account_id"], ["platform_account.id"],
        ),
        sa.UniqueConstraint(
            "knowledge_base_id", "revision_no",
            name="uq_knowledge_lexicon_revision_no",
        ),
        sa.UniqueConstraint(
            "knowledge_base_id", "content_hash",
            name="uq_knowledge_lexicon_content_hash",
        ),
        sa.UniqueConstraint(
            "id", "tenant_id", name="uq_knowledge_lexicon_id_tenant",
        ),
    )
    op.create_index(
        "ix_knowledge_lexicon_tenant_base_revision",
        "knowledge_lexicon_revision",
        ["tenant_id", "knowledge_base_id", "revision_no"],
    )

    op.add_column(
        "knowledge_base", sa.Column("current_lexicon_revision_id", sa.String(36)),
    )
    op.add_column(
        "knowledge_base",
        sa.Column(
            "next_lexicon_revision_no", sa.BigInteger(), nullable=False,
            server_default="1",
        ),
    )
    op.create_foreign_key(
        "fk_knowledge_base_current_lexicon", "knowledge_base",
        "knowledge_lexicon_revision", ["current_lexicon_revision_id"], ["id"],
    )

    op.add_column(
        "knowledge_index_revision", sa.Column("lexicon_revision_id", sa.String(36)),
    )
    op.add_column(
        "knowledge_index_revision",
        sa.Column("retrieval_profile", sa.JSON(), nullable=True),
    )
    op.execute(sa.text(
        "UPDATE knowledge_index_revision SET retrieval_profile = '{}' "
        "WHERE retrieval_profile IS NULL"
    ))
    op.alter_column(
        "knowledge_index_revision", "retrieval_profile",
        existing_type=sa.JSON(), nullable=False,
    )
    op.create_foreign_key(
        "fk_knowledge_index_lexicon_revision", "knowledge_index_revision",
        "knowledge_lexicon_revision", ["lexicon_revision_id"], ["id"],
    )
    op.add_column(
        "knowledge_chunk",
        sa.Column("lexicon_term_ids", sa.JSON(), nullable=True),
    )
    op.execute(sa.text(
        "UPDATE knowledge_chunk SET lexicon_term_ids = '[]' "
        "WHERE lexicon_term_ids IS NULL"
    ))
    op.alter_column(
        "knowledge_chunk", "lexicon_term_ids",
        existing_type=sa.JSON(), nullable=False,
    )
    op.add_column(
        "knowledge_retrieval_snapshot",
        sa.Column("retrieval_profile", sa.JSON(), nullable=True),
    )
    op.execute(sa.text(
        "UPDATE knowledge_retrieval_snapshot SET retrieval_profile = '{}' "
        "WHERE retrieval_profile IS NULL"
    ))
    op.alter_column(
        "knowledge_retrieval_snapshot", "retrieval_profile",
        existing_type=sa.JSON(), nullable=False,
    )
    op.add_column(
        "knowledge_retrieval_snapshot",
        sa.Column("degraded_routes", sa.JSON(), nullable=True),
    )
    op.execute(sa.text(
        "UPDATE knowledge_retrieval_snapshot SET degraded_routes = '[]' "
        "WHERE degraded_routes IS NULL"
    ))
    op.alter_column(
        "knowledge_retrieval_snapshot", "degraded_routes",
        existing_type=sa.JSON(), nullable=False,
    )


def downgrade() -> None:
    op.drop_column("knowledge_retrieval_snapshot", "degraded_routes")
    op.drop_column("knowledge_retrieval_snapshot", "retrieval_profile")
    op.drop_column("knowledge_chunk", "lexicon_term_ids")
    op.drop_constraint(
        "fk_knowledge_index_lexicon_revision", "knowledge_index_revision",
        type_="foreignkey",
    )
    op.drop_column("knowledge_index_revision", "retrieval_profile")
    op.drop_column("knowledge_index_revision", "lexicon_revision_id")
    op.drop_constraint(
        "fk_knowledge_base_current_lexicon", "knowledge_base", type_="foreignkey",
    )
    op.drop_column("knowledge_base", "next_lexicon_revision_no")
    op.drop_column("knowledge_base", "current_lexicon_revision_id")
    op.drop_table("knowledge_lexicon_revision")
