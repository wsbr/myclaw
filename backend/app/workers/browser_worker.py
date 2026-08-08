"""Dedicated durable worker for isolated Chromium Tool invocations."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import redis.asyncio as aioredis

from app.config import settings
from app.services.browser_worker_service import browser_manager
from app.workers.sandbox_worker import SandboxWorker


class BrowserWorker(SandboxWorker):
    def __init__(self, worker_id: str | None = None):
        super().__init__(worker_id, worker_kind="browser")

    async def _heartbeat(self, ready: asyncio.Event) -> None:
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            while True:
                await client.set(
                    "myclaw:browser-worker:heartbeat",
                    json.dumps({
                        "worker_id": self.worker_id,
                        "engine": "chromium",
                        "isolated_contexts": True,
                        "ready": ready.is_set(),
                        "at": datetime.now(UTC).isoformat(),
                    }, sort_keys=True),
                    ex=10,
                )
                await asyncio.sleep(2)
        finally:
            await client.aclose()

    async def run_forever(self, poll_seconds: float | None = None) -> None:
        ready = asyncio.Event()
        await browser_manager._launch()
        ready.set()
        heartbeat = asyncio.create_task(self._heartbeat(ready))
        try:
            await super().run_forever(poll_seconds)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)


async def main() -> None:
    await BrowserWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(main())
