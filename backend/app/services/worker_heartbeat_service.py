"""Best-effort process heartbeat independent from Runner and Tool leases."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Callable

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.common import utcnow
from app.models.runtime_v1 import WorkerInstance


logger = logging.getLogger(__name__)


async def record_worker_heartbeat(
    *, worker_id: str, worker_kind: str, capacity: int, active_slots: int,
    stopped: bool = False,
) -> None:
    """Upsert one bounded process observation without touching execution state."""

    capacity = max(1, int(capacity))
    active_slots = max(0, min(int(active_slots), capacity))
    now = utcnow()
    async with AsyncSessionLocal.begin() as db:
        item = await db.scalar(select(WorkerInstance).where(
            WorkerInstance.worker_id == worker_id,
        ).with_for_update())
        if item is None:
            item = WorkerInstance(
                worker_id=worker_id, worker_kind=worker_kind,
                capacity=capacity, active_slots=active_slots,
                status="stopped" if stopped else "active",
                hostname=socket.gethostname(), process_id=os.getpid(),
                release_id=settings.myclaw_release,
                started_at=now, heartbeat_at=now,
                stopped_at=now if stopped else None,
            )
            db.add(item)
            return
        item.worker_kind = worker_kind
        item.capacity = capacity
        item.active_slots = active_slots
        item.status = "stopped" if stopped else "active"
        item.hostname = socket.gethostname()
        item.process_id = os.getpid()
        item.release_id = settings.myclaw_release
        item.heartbeat_at = now
        item.stopped_at = now if stopped else None


async def run_worker_heartbeat(
    *, worker_id: str, worker_kind: str, capacity: int,
    active_slots: Callable[[], int], stop: asyncio.Event,
) -> None:
    """Publish liveness while idle as well as busy; failures never stop work."""

    interval = float(settings.worker_heartbeat_seconds)
    while not stop.is_set():
        try:
            await record_worker_heartbeat(
                worker_id=worker_id, worker_kind=worker_kind,
                capacity=capacity, active_slots=active_slots(),
            )
        except Exception:
            logger.exception(
                "Worker heartbeat persistence failed",
                extra={"worker_id": worker_id, "worker_kind": worker_kind},
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
    try:
        await record_worker_heartbeat(
            worker_id=worker_id, worker_kind=worker_kind,
            capacity=capacity, active_slots=0, stopped=True,
        )
    except Exception:
        logger.exception(
            "Worker stop heartbeat persistence failed",
            extra={"worker_id": worker_id, "worker_kind": worker_kind},
        )
