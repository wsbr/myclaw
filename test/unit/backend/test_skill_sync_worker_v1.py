"""Durable publication-to-cache Skill synchronization."""

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.foundation import OutboxCommand
from app.models.registry import SkillVersion
from app.workers.skill_sync_worker import SkillSyncWorker


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_sync_worker_consumes_publication_outbox(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()
    async with factory() as db:
        db.add_all([
            SkillVersion(
                id="sv1", skill_id="skill1", version_no=1,
                status="published", package_uri="skill-package://skill1/sv1/package.zip",
                checksum="a" * 64, manifest={}, content_hash="h" * 64,
                manifest_name="Writer", manifest_description="Write",
                created_by_account_id="account1", published_at=now,
            ),
            OutboxCommand(
                id="cmd1", topic="runtime.maintenance", action="sync_skill",
                aggregate_type="skill_version", aggregate_id="sv1",
                partition_key="skill1", dedupe_key="skill_version:sv1:sync",
                payload_ref={"skill_id": "skill1", "version_id": "sv1"},
                priority=100, available_at=now, status="pending",
            ),
        ])
        await db.commit()

    calls = []

    def fake_sync(skill_id, checksum, package_uri):
        calls.append((skill_id, checksum, package_uri))
        return f"skill://{skill_id}/{checksum}/"

    monkeypatch.setattr("app.workers.skill_sync_worker.AsyncSessionLocal", factory)
    monkeypatch.setattr("app.workers.skill_sync_worker.sync_package", fake_sync)
    assert await SkillSyncWorker(worker_id="test-worker").run_once()
    assert calls == [("skill1", "a" * 64, "skill-package://skill1/sv1/package.zip")]
    async with factory() as db:
        command = await db.get(OutboxCommand, "cmd1")
        assert command.status == "completed"
        assert command.completed_at is not None
        assert command.dispatch_lease_token is None
    await engine.dispose()
