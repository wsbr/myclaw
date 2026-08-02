"""Foreign-key-safe persistence helpers for WorkItem/Runner graphs."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_v1 import DurableRunner, WorkItem


async def persist_new_work_runner(
    db: AsyncSession, work: WorkItem, runner: DurableRunner,
) -> None:
    """Insert a new WorkItem/Runner pair despite their intentional FK cycle."""
    work.active_runner_id = None
    db.add(work)
    await db.flush()
    db.add(runner)
    await db.flush()
    work.active_runner_id = runner.id
    await db.flush()


async def persist_replacement_runner(
    db: AsyncSession, work: WorkItem, runner: DurableRunner,
) -> None:
    """Insert a Runner for an existing WorkItem before switching its pointer."""
    db.add(runner)
    await db.flush()
    work.active_runner_id = runner.id
    await db.flush()
