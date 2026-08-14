"""Retention leader plus durable History/Memory maintenance consumers."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from app.config import settings
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import engine
from app.models.common import uuid7
from app.services.retention_service import run_retention_cleanup
from app.services.maintenance_command_service import MaintenanceCommandWorker
from app.services.agent_eval_service import reconcile_one_eval_case
from app.services.worker_heartbeat_service import run_worker_heartbeat


logger = logging.getLogger(__name__)
_LOCK_NAME = "myclaw.retention_cleanup"


class _Activity:
    def __init__(self) -> None:
        self.count = 0

    def enter(self) -> None:
        self.count += 1

    def exit(self) -> None:
        self.count = max(0, self.count - 1)


async def run_once() -> bool:
    async with engine.connect() as connection:
        mysql = connection.dialect.name == "mysql"
        acquired = True
        if mysql:
            acquired = bool(await connection.scalar(
                text("SELECT GET_LOCK(:name, 0)"), {"name": _LOCK_NAME},
            ))
            await connection.commit()
        if not acquired:
            return False
        try:
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                async with db.begin():
                    result = await run_retention_cleanup(db, job_id=uuid7())
            logger.info("retention cleanup completed", extra={"deleted_counts": result.__dict__})
            return True
        finally:
            if mysql:
                await connection.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": _LOCK_NAME})
                await connection.commit()


async def _retention_loop(activity: _Activity) -> None:
    while True:
        activity.enter()
        try:
            await run_once()
        except Exception:
            logger.exception("retention cleanup failed")
        finally:
            activity.exit()
        await asyncio.sleep(settings.maintenance_poll_seconds)


async def _command_loop(worker: MaintenanceCommandWorker, activity: _Activity) -> None:
    while True:
        activity.enter()
        try:
            worked = await worker.run_once()
        except Exception:
            logger.exception("maintenance command iteration failed")
            worked = False
        finally:
            activity.exit()
        if not worked:
            await asyncio.sleep(settings.maintenance_command_poll_seconds)


async def _agent_eval_loop(activity: _Activity) -> None:
    while True:
        activity.enter()
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                async with db.begin():
                    worked = await reconcile_one_eval_case(db)
        except Exception:
            logger.exception("Agent eval reconciliation iteration failed")
            worked = False
        finally:
            activity.exit()
        if not worked:
            await asyncio.sleep(settings.maintenance_command_poll_seconds)


async def main() -> None:
    worker = MaintenanceCommandWorker()
    activity = _Activity()
    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(run_worker_heartbeat(
        worker_id=worker.worker_id, worker_kind="maintenance", capacity=3,
        active_slots=lambda: activity.count, stop=heartbeat_stop,
    ), name=f"worker-heartbeat:{worker.worker_id}")
    try:
        await asyncio.gather(
            _retention_loop(activity), _command_loop(worker, activity),
            _agent_eval_loop(activity),
        )
    finally:
        heartbeat_stop.set()
        await asyncio.gather(heartbeat, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
