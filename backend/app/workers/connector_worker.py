"""Dedicated durable worker for outbound Web and MCP connector calls."""

from __future__ import annotations

import asyncio

from app.workers.sandbox_worker import SandboxWorker


class ConnectorWorker(SandboxWorker):
    def __init__(self, worker_id: str | None = None):
        super().__init__(worker_id, worker_kind="connector")


async def main() -> None:
    await ConnectorWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(main())
