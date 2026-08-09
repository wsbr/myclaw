"""Skill package metadata and immutable version enforcement.

Revision ID: 0032
Revises: 0031
"""

from alembic import op
import sqlalchemy as sa


revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skill_version", sa.Column("package_size", sa.BigInteger()))
    op.add_column("skill_version", sa.Column("archive_type", sa.String(20)))
    op.execute(sa.text("""
        UPDATE skill_version
        SET manifest = JSON_OBJECT(
              'schema_version', 'skill.manifest.v1',
              'name', manifest_name,
              'description', manifest_description,
              'archive_type', CASE
                  WHEN package_uri LIKE '%.tar.gz' OR package_uri LIKE '%.tgz' THEN 'tar.gz'
                  ELSE 'zip'
              END
            ),
            archive_type = CASE
                WHEN package_uri LIKE '%.tar.gz' OR package_uri LIKE '%.tgz' THEN 'tar.gz'
                ELSE 'zip'
            END,
            content_hash = SHA2(CONCAT(
              id, ':', skill_id, ':', checksum, ':', package_uri, ':',
              CAST(JSON_OBJECT(
                'schema_version', 'skill.manifest.v1',
                'name', manifest_name,
                'description', manifest_description,
                'archive_type', CASE
                    WHEN package_uri LIKE '%.tar.gz' OR package_uri LIKE '%.tgz' THEN 'tar.gz'
                    ELSE 'zip'
                END
              ) AS CHAR)
            ), 256)
    """))
    op.execute("""
        CREATE TRIGGER trg_skill_version_immutable_update
        BEFORE UPDATE ON skill_version FOR EACH ROW
        BEGIN
          IF NOT (
            OLD.id <=> NEW.id
            AND OLD.skill_id <=> NEW.skill_id
            AND OLD.version_no <=> NEW.version_no
            AND OLD.package_uri <=> NEW.package_uri
            AND OLD.checksum <=> NEW.checksum
            AND OLD.manifest <=> NEW.manifest
            AND OLD.content_hash <=> NEW.content_hash
            AND OLD.package_size <=> NEW.package_size
            AND OLD.archive_type <=> NEW.archive_type
            AND OLD.manifest_name <=> NEW.manifest_name
            AND OLD.manifest_description <=> NEW.manifest_description
            AND OLD.created_by_account_id <=> NEW.created_by_account_id
            AND OLD.created_at <=> NEW.created_at
          ) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'skill_version content is immutable';
          END IF;
          IF NOT (
            OLD.status = NEW.status
            OR (OLD.status = 'uploaded' AND NEW.status IN ('validating','published','rejected','quarantined'))
            OR (OLD.status = 'validating' AND NEW.status IN ('published','rejected','quarantined'))
            OR (OLD.status = 'published' AND NEW.status IN ('deprecated','quarantined'))
            OR (OLD.status = 'deprecated' AND NEW.status = 'quarantined')
          ) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid skill_version status transition';
          END IF;
          IF OLD.published_at IS NOT NULL AND NOT (OLD.published_at <=> NEW.published_at) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'skill_version published_at is immutable once set';
          END IF;
          IF NEW.status IN ('published','deprecated') AND NEW.published_at IS NULL THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'published skill_version requires published_at';
          END IF;
          IF OLD.published_at IS NULL AND NEW.published_at IS NOT NULL AND NEW.status <> 'published' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'published_at may only be set while publishing';
          END IF;
          IF OLD.status IN ('published','deprecated','rejected','quarantined')
             AND NOT (OLD.validation_error <=> NEW.validation_error) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'terminal skill_version validation is immutable';
          END IF;
        END
    """)
    op.execute(
        "CREATE TRIGGER trg_skill_version_immutable_delete BEFORE DELETE ON skill_version "
        "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'skill_version is append-only'"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_skill_version_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_skill_version_immutable_update")
    op.drop_column("skill_version", "archive_type")
    op.drop_column("skill_version", "package_size")
