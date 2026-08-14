"""Process heartbeat remains visible independently from execution leases."""

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.common import utcnow
from app.models.runtime_v1 import WorkerInstance
from app.services.runtime_console_service import runtime_console_summary
from app.services.worker_heartbeat_service import record_worker_heartbeat
import app.services.worker_heartbeat_service as heartbeat_service


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_idle_worker_registration_health_and_clean_stop(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(heartbeat_service, "AsyncSessionLocal", factory)

    await record_worker_heartbeat(
        worker_id="runtime:test:1", worker_kind="runtime",
        capacity=8, active_slots=0,
    )
    async with factory() as db:
        summary = await runtime_console_summary(db)
        assert summary["observed_workers"] == [{
            "worker_kind": "runtime",
            "worker_id": "runtime:test:1",
            "capacity": 8,
            "active_slots": 0,
            "observed_active_slots": 0,
            "last_heartbeat_at": summary["observed_workers"][0]["last_heartbeat_at"],
            "stale_leases": 0,
            "health": "active",
            "release_id": settings.myclaw_release,
            "hostname": summary["observed_workers"][0]["hostname"],
            "process_id": summary["observed_workers"][0]["process_id"],
        }]

    async with factory.begin() as db:
        item = await db.get(WorkerInstance, "runtime:test:1")
        item.heartbeat_at = utcnow() - timedelta(
            seconds=settings.worker_heartbeat_stale_seconds + 1,
        )
    async with factory() as db:
        summary = await runtime_console_summary(db)
        assert summary["observed_workers"][0]["health"] == "stale"

    await record_worker_heartbeat(
        worker_id="runtime:test:1", worker_kind="runtime",
        capacity=8, active_slots=0, stopped=True,
    )
    async with factory() as db:
        summary = await runtime_console_summary(db)
        assert summary["observed_workers"][0]["health"] == "stopped"
    await engine.dispose()
