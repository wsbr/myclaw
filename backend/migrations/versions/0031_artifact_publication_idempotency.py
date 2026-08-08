"""atomically idempotent Artifact publication

Revision ID: 0031
Revises: 0030
"""

from alembic import op
import sqlalchemy as sa


revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifact", sa.Column("publication_key", sa.String(64), nullable=True))
    # Legacy/UAT rows predate semantic publication keys. Keep them addressable
    # without falsely deduplicating independently committed historical rows.
    op.execute(sa.text(
        "UPDATE artifact SET publication_key = SHA2(CONCAT('legacy:', id), 256) "
        "WHERE publication_key IS NULL"
    ))
    op.alter_column(
        "artifact", "publication_key", existing_type=sa.String(64), nullable=False,
    )
    op.create_unique_constraint(
        "uq_artifact_publication_key", "artifact", ["publication_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_artifact_publication_key", "artifact", type_="unique")
    op.drop_column("artifact", "publication_key")
