"""Dedicated schedule controller; all actual work is delegated to Runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import socket

from app.config import settings
from app.services.schedule_service import (
    claim_dispatchable_fire,
    claim_due_schedule,
    dispatch_fire_claim,
    materialize_schedule_claim,
    reconcile_one_fire,
)
from app.services.worker_heartbeat_service import run_worker_heartbeat


logger = logging.getLogger(__name__)


class SchedulerWorker:
    def __init__(self, worker_id: str | None = None) -> None:
        self.worker_id = worker_id or f"scheduler:{socket.gethostname()}:{os.getpid()}"
        self._active_slots = 0
        self._semaphore = asyncio.Semaphore(settings.scheduler_worker_concurrency)

    async def _run_bounded(self, operation, claim) -> None:
        async with self._semaphore:
            self._active_slots += 1
            try:
                await operation(claim)
            except Exception:
                logger.exception("scheduler operation failed")
            finally:
                self._active_slots = max(0, self._active_slots - 1)

    async def run(self) -> None:
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(run_worker_heartbeat(
            worker_id=self.worker_id, worker_kind="scheduler",
            capacity=settings.scheduler_worker_concurrency,
            active_slots=lambda: self._active_slots, stop=heartbeat_stop,
        ), name=f"worker-heartbeat:{self.worker_id}")
        tasks: set[asyncio.Task] = set()
        try:
            while True:
                worked = False
                try:
                    while await reconcile_one_fire():
                        worked = True
                    if len(tasks) < settings.scheduler_worker_concurrency:
                        fire_claim = await claim_dispatchable_fire(self.worker_id)
                        if fire_claim is not None:
                            worked = True
                            task = asyncio.create_task(
                                self._run_bounded(dispatch_fire_claim, fire_claim)
                            )
                            tasks.add(task)
                            task.add_done_callback(tasks.discard)
                    schedule_claim = await claim_due_schedule(self.worker_id)
                    if schedule_claim is not None:
                        worked = True
                        await materialize_schedule_claim(schedule_claim)
                except Exception:
                    logger.exception("scheduler iteration failed")
                if not worked:
                    await asyncio.sleep(settings.scheduler_poll_seconds)
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            heartbeat_stop.set()
            await asyncio.gather(heartbeat, return_exceptions=True)


async def main() -> None:
    await SchedulerWorker().run()


if __name__ == "__main__":
    asyncio.run(main())
