"""Real MySQL enforcement for immutable SkillVersion content."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_skill_version_content_and_status_are_database_enforced():
    url = os.environ.get("MYCLAW_V3_ACCEPTANCE_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL acceptance database is not configured")
    engine = create_async_engine(url)
    skill_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            await connection.execute(text("""
                INSERT INTO skill
                  (id, logical_name, name, description, owner_account_id, status,
                   current_published_version_id, version, created_at, updated_at)
                VALUES
                  (:id, :logical, 'Integrity Skill', 'Trigger evidence', :account,
                   'active', NULL, 1, UTC_TIMESTAMP(), UTC_TIMESTAMP())
            """), {
                "id": skill_id,
                "logical": f"integrity-{skill_id}",
                "account": account_id,
            })
            await connection.execute(text("""
                INSERT INTO skill_version
                  (id, skill_id, version_no, status, package_uri, checksum,
                   manifest, content_hash, package_size, archive_type,
                   manifest_name, manifest_description, created_by_account_id,
                   published_at, created_at)
                VALUES
                  (:id, :skill_id, 1, 'uploaded', :uri, :checksum,
                   JSON_OBJECT('name','Integrity Skill','description','Trigger evidence'),
                   :content_hash, 128, 'zip', 'Integrity Skill', 'Trigger evidence',
                   :account, NULL, UTC_TIMESTAMP())
            """), {
                "id": version_id,
                "skill_id": skill_id,
                "uri": f"s3://test/{version_id}.zip",
                "checksum": "a" * 64,
                "content_hash": "b" * 64,
                "account": account_id,
            })
            await connection.execute(text("SET FOREIGN_KEY_CHECKS=1"))

        async with engine.begin() as connection:
            await connection.execute(text(
                "UPDATE skill_version SET status='published', published_at=UTC_TIMESTAMP() WHERE id=:id"
            ), {"id": version_id})

        async with engine.begin() as connection:
            with pytest.raises(DBAPIError, match="skill_version content is immutable"):
                await connection.execute(text(
                    "UPDATE skill_version SET package_uri='s3://test/replaced.zip' WHERE id=:id"
                ), {"id": version_id})

        async with engine.begin() as connection:
            with pytest.raises(DBAPIError, match="invalid skill_version status transition"):
                await connection.execute(text(
                    "UPDATE skill_version SET status='uploaded' WHERE id=:id"
                ), {"id": version_id})

        async with engine.begin() as connection:
            with pytest.raises(DBAPIError, match="skill_version is append-only"):
                await connection.execute(text(
                    "DELETE FROM skill_version WHERE id=:id"
                ), {"id": version_id})
    finally:
        await engine.dispose()
