"""Lease, fencing, outbox acknowledgement, and recovery tests for v2 worker."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.common import utcnow
from app.models.foundation import OutboxCommand
from app.models.runtime_v1 import DurableRunner, RunnerCheckpoint, WorkItem
from app.workers.runtime_worker import RunnerFenced, RuntimeWorker
from unit.backend.runtime_worker_testkit import create_runtime_world

pytestmark = pytest.mark.unit


async def _patch_factory(monkeypatch, factory):
    import app.workers.runtime_worker as module
    monkeypatch.setattr(module, "AsyncSessionLocal", factory)


@pytest.mark.asyncio
async def test_claim_is_exclusive_and_runner_acquire_acknowledges_outbox(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    first = RuntimeWorker("worker-a")
    second = RuntimeWorker("worker-b")
    claim = await first.claim_one()
    assert claim is not None
    assert await second.claim_one() is None
    fence = await first.acquire_runner(claim)
    assert fence is not None
    async with world.factory() as db:
        command = await db.get(OutboxCommand, claim.command_id)
        runner = await db.get(DurableRunner, world.runner_id)
        assert command.status == "completed"
        assert runner.status == "running"
        assert runner.lease_token == fence.lease_token
        assert runner.fencing_token == fence.execution_epoch
    await world.engine.dispose()


@pytest.mark.asyncio
async def test_expired_lease_recovery_increments_epoch_and_fences_late_writer(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    original = RuntimeWorker("original")
    claim = await original.claim_one()
    fence = await original.acquire_runner(claim)
    assert fence is not None
    async with world.factory.begin() as db:
        runner = await db.get(DurableRunner, world.runner_id)
        runner.lease_expires_at = utcnow() - timedelta(seconds=1)

    recovery = RuntimeWorker("recovery")
    assert await recovery.recover_one_expired_runner() is True
    assert await original.heartbeat(fence) is False
    with pytest.raises(RunnerFenced):
        await original._settle_completed(fence, "late", type("C", (), {
            "manifest": (), "compaction": None,
        })())
    async with world.factory() as db:
        work = await db.get(WorkItem, world.work_id)
        runners = list(await db.scalars(select(DurableRunner).where(
            DurableRunner.work_item_id == world.work_id,
        ).order_by(DurableRunner.execution_epoch)))
        assert work.execution_epoch == 2
        assert work.active_runner_id == runners[1].id
        assert [runner.status for runner in runners] == ["lost", "queued"]
        assert runners[1].input_snapshot["recovered_from_checkpoint_id"]
        assert list(await db.scalars(select(RunnerCheckpoint).where(
            RunnerCheckpoint.runner_id == runners[0].id,
        )))
        pending = await db.scalar(select(OutboxCommand).where(
            OutboxCommand.status == "pending",
        ))
        assert pending is not None and pending.aggregate_id == runners[1].id
    await world.engine.dispose()


@pytest.mark.asyncio
async def test_recovery_carries_latest_persisted_tool_continuation(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    worker = RuntimeWorker("continuation-source")
    claim = await worker.claim_one()
    fence = await worker.acquire_runner(claim)
    continuation = [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call-1", "type": "tool_use", "name": "lookup",
            "input": {"key": "alpha"},
        }]},
        {"role": "tool", "tool_use_id": "call-1", "content": '{"value": 42}'},
    ]
    async with world.factory.begin() as db:
        db.add(RunnerCheckpoint(
            runner_id=world.runner_id, work_item_id=world.work_id,
            sequence_no=2, checkpoint_type="tool_results_persisted",
            state_snapshot={"pending_results": continuation},
            execution_epoch=1, context_manifest={}, result_cursor={},
            pending_refs={}, budget_snapshot={},
            last_completed_step="tool_results_persisted", safe_to_resume=True,
        ))
        runner = await db.get(DurableRunner, world.runner_id)
        runner.lease_expires_at = utcnow() - timedelta(seconds=1)

    assert await RuntimeWorker("continuation-recovery").recover_one_expired_runner() is True
    async with world.factory() as db:
        runners = list(await db.scalars(select(DurableRunner).where(
            DurableRunner.work_item_id == world.work_id,
        ).order_by(DurableRunner.execution_epoch)))
        assert runners[-1].input_snapshot["pending_results"] == continuation
    await world.engine.dispose()
