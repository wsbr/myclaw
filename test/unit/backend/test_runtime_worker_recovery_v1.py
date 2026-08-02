from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy import func, select

from app.api.runs import cancel_run
from app.deps import CurrentAccount
from app.models.common import utcnow
from app.models.foundation import ExecutionEvent, OutboxCommand, PlatformAccount
from app.models.runtime_v1 import (
    AgentInvocation, DurableRunner, Message, Run, RunnerCheckpoint, ToolInvocation, WorkItem,
)
from app.schemas.runtime_v1 import RunCancelRequest
from app.workers.runtime_worker_v1 import RunnerFence, RuntimeWorker
from unit.backend.runtime_worker_testkit import create_runtime_world


pytestmark = pytest.mark.unit


async def _patch_factory(monkeypatch, factory):
    import app.services.observability_service as observability
    import app.workers.runtime_worker_v1 as worker_module

    monkeypatch.setattr(worker_module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(observability, "AsyncSessionLocal", factory)


@pytest.mark.asyncio
async def test_two_workers_cannot_claim_the_same_unexpired_delivery(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    first_worker, second_worker = RuntimeWorker("worker-a"), RuntimeWorker("worker-b")
    first = await first_worker.claim_one()
    second = await second_worker.claim_one()
    assert first is not None
    assert second is None
    async with world.factory() as db:
        command = await db.get(OutboxCommand, first.command_id)
        assert command.status == "claimed"
        assert command.dispatch_lease_owner == "worker-a"
        assert command.attempt_count == 1
    await world.engine.dispose()


@pytest.mark.asyncio
async def test_acknowledgement_is_token_fenced_and_not_repeatable(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    worker = RuntimeWorker("worker")
    claim = await worker.claim_one()
    assert claim is not None
    assert await worker.acknowledge(claim) is True
    assert await worker.acknowledge(claim) is False
    forged = type(claim)(claim.command_id, "forged-token", claim.runner_id)
    assert await worker.acquire_runner(forged) is None
    await world.engine.dispose()


@pytest.mark.asyncio
async def test_expired_runner_recovers_from_checkpoint_with_new_epoch_and_fences_old_worker(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    worker = RuntimeWorker("original")
    claim = await worker.claim_one()
    fence = await worker.acquire_runner(claim)
    assert fence is not None and await worker.acknowledge(claim)
    async with world.factory.begin() as db:
        runner = await db.get(DurableRunner, world.runner_id)
        runner.lease_expires_at = utcnow() - timedelta(seconds=1)

    replacement_worker = RuntimeWorker("replacement")
    assert await replacement_worker.recover_one_expired_runner() is True
    assert await worker.heartbeat(fence) is False
    with pytest.raises(RuntimeError, match="fencing conflict"):
        await worker._settle_success(fence, "late result")

    async with world.factory() as db:
        work = await db.get(WorkItem, world.work_id)
        runners = list(await db.scalars(select(DurableRunner).where(
            DurableRunner.work_item_id == world.work_id,
        ).order_by(DurableRunner.execution_epoch)))
        assert work.status == "queued"
        assert work.execution_epoch == 2 and work.attempt_no == 2
        assert [item.status for item in runners] == ["lost", "queued"]
        assert [item.execution_epoch for item in runners] == [1, 2]
        assert runners[1].input_snapshot["resumed_from_runner_id"] == runners[0].id
        assert await db.scalar(select(func.count()).select_from(RunnerCheckpoint)) == 1
        pending = await db.scalar(select(OutboxCommand).where(OutboxCommand.status == "pending"))
        assert pending is not None and pending.aggregate_id == runners[1].id
    await world.engine.dispose()


@pytest.mark.asyncio
async def test_expired_runner_without_checkpoint_fails_once_and_duplicate_delivery_cannot_restart_it(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    async with world.factory.begin() as db:
        run = await db.get(Run, world.run_id)
        work = await db.get(WorkItem, world.work_id)
        runner = await db.get(DurableRunner, world.runner_id)
        runner.status = "running"
        runner.lease_owner = "lost"
        runner.lease_token = "lost-token"
        runner.lease_expires_at = utcnow() - timedelta(seconds=1)
        work.status = "running"
        run.lifecycle = "running"

    worker = RuntimeWorker("recovery")
    assert await worker.recover_one_expired_runner() is True
    claim = await worker.claim_one()
    assert claim is not None
    assert await worker.acquire_runner(claim) is None
    async with world.factory() as db:
        run = await db.get(Run, world.run_id)
        work = await db.get(WorkItem, world.work_id)
        runners = list(await db.scalars(select(DurableRunner).where(
            DurableRunner.work_item_id == world.work_id,
        )))
        assert run.lifecycle == "completed" and run.outcome == "failed"
        assert run.error_code == "runner_lost_without_checkpoint"
        assert work.status == "failed" and work.active_runner_id is None
        assert len(runners) == 1 and runners[0].status == "lost"
        terminal = list(await db.scalars(select(ExecutionEvent).where(
            ExecutionEvent.run_id == world.run_id,
            ExecutionEvent.event_type == "run.completed",
        )))
        assert len(terminal) == 1
    await world.engine.dispose()


@pytest.mark.asyncio
async def test_expired_runner_after_terminal_run_is_observed_but_never_resumed(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    worker = RuntimeWorker("original")
    claim = await worker.claim_one()
    fence = await worker.acquire_runner(claim)
    assert fence is not None and await worker.acknowledge(claim)
    async with world.factory.begin() as db:
        run = await db.get(Run, world.run_id)
        runner = await db.get(DurableRunner, world.runner_id)
        run.lifecycle = "cancelled"
        run.wait_reason = None
        run.outcome = None
        run.finished_at = utcnow()
        runner.lease_expires_at = utcnow() - timedelta(seconds=1)

    assert await RuntimeWorker("recovery").recover_one_expired_runner() is True
    with pytest.raises(RuntimeError, match="fencing conflict"):
        await worker._settle_success(fence, "late result")
    async with world.factory() as db:
        work = await db.get(WorkItem, world.work_id)
        runners = list(await db.scalars(select(DurableRunner).where(
            DurableRunner.work_item_id == world.work_id,
        )))
        assert work.active_runner_id is None
        assert len(runners) == 1 and runners[0].status == "lost"
        assert await db.scalar(select(func.count()).select_from(OutboxCommand)) == 1
    await world.engine.dispose()


@pytest.mark.asyncio
async def test_stale_expired_runner_cannot_overwrite_newer_active_epoch(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    async with world.factory.begin() as db:
        work = await db.get(WorkItem, world.work_id)
        runner = await db.get(DurableRunner, world.runner_id)
        runner.status = "running"
        runner.lease_token = "stale"
        runner.lease_expires_at = utcnow() - timedelta(seconds=1)
        work.active_runner_id = "newer-runner"
        work.execution_epoch = 2
        work.status = "running"

    assert await RuntimeWorker("recovery").recover_one_expired_runner() is True
    async with world.factory() as db:
        work = await db.get(WorkItem, world.work_id)
        runner = await db.get(DurableRunner, world.runner_id)
        assert runner.status == "lost"
        assert work.active_runner_id == "newer-runner"
        assert work.execution_epoch == 2
        assert await db.scalar(select(func.count()).select_from(DurableRunner)) == 1
    await world.engine.dispose()


@pytest.mark.asyncio
async def test_cancel_wins_against_running_worker_and_late_success_is_fenced(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    worker = RuntimeWorker("running-worker")
    claim = await worker.claim_one()
    fence = await worker.acquire_runner(claim)
    assert fence is not None and await worker.acknowledge(claim)
    async with world.factory() as db:
        account = await db.scalar(select(PlatformAccount))
        current = CurrentAccount(account=account, roles=frozenset({"user"}))
        cancelled = await cancel_run(
            world.run_id, RunCancelRequest(reason="race cancellation"),
            if_match='"1"', idempotency_key="cancel-race", db=db, current=current,
        )
    assert cancelled.lifecycle == "cancelled"
    with pytest.raises(RuntimeError, match="fencing conflict"):
        await worker._settle_success(fence, "late model result")

    async with world.factory() as db:
        run = await db.get(Run, world.run_id)
        runner = await db.get(DurableRunner, world.runner_id)
        work = await db.get(WorkItem, world.work_id)
        assert run.lifecycle == "cancelled" and run.outcome is None
        assert runner.status == "cancelled"
        assert work.status == "cancelled" and work.active_runner_id is None
        assert await db.scalar(select(func.count()).select_from(Message)) == 1
        assert await db.scalar(select(func.count()).select_from(ExecutionEvent).where(
            ExecutionEvent.event_type == "run.cancelled",
        )) == 1
        assert await db.scalar(select(func.count()).select_from(ExecutionEvent).where(
            ExecutionEvent.event_type == "run.completed",
        )) == 0
    await world.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mysql_code", [1205, 1213])
async def test_transient_mysql_settlement_conflict_retries_without_business_failure(
    monkeypatch, mysql_code,
):
    worker = RuntimeWorker("retry-worker")
    fence = RunnerFence("runner", "work", "run", "session", 1, "lease")
    attempts = 0

    async def flaky_settlement(current_fence, content):
        nonlocal attempts
        attempts += 1
        assert current_fence == fence and content == "result"
        if attempts < 3:
            raise OperationalError(
                "SELECT run FOR UPDATE", {}, RuntimeError(mysql_code, "lock conflict"),
            )

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(worker, "_settle_success_once", flaky_settlement)
    monkeypatch.setattr("app.workers.runtime_worker_v1.asyncio.sleep", no_delay)
    await worker._settle_success(fence, "result")
    assert attempts == 3


@pytest.mark.asyncio
async def test_non_transient_settlement_error_is_not_retried(monkeypatch):
    worker = RuntimeWorker("retry-worker")
    fence = RunnerFence("runner", "work", "run", "session", 1, "lease")
    attempts = 0

    async def invalid_settlement(_fence, _content):
        nonlocal attempts
        attempts += 1
        raise OperationalError("SELECT", {}, RuntimeError(9999, "not retryable"))

    monkeypatch.setattr(worker, "_settle_success_once", invalid_settlement)
    with pytest.raises(OperationalError):
        await worker._settle_success(fence, "result")
    assert attempts == 1


@pytest.mark.asyncio
async def test_plan_coordination_settles_after_child_without_resuming_owner_model(monkeypatch):
    """A frozen Plan delegation shell must not invoke the Owner again after its child settles."""
    import app.workers.runtime_worker_v1 as worker_module

    run = Run(
        id="run", session_id="session", user_id="user", mode="plan", execution_kind="normal",
        lifecycle="waiting", wait_reason="waiting_agent", entry_agent_id="owner", version=3,
    )
    target_runner = DurableRunner(
        id="target-runner", work_item_id="target-work", run_id="run", session_id="session",
        agent_id="child", agent_version_id="child-version", execution_epoch=1, status="running",
        principal_snapshot={}, assembly_snapshot={}, prompt_manifest={}, input_snapshot={}, version=1,
    )
    target_work = WorkItem(
        id="target-work", run_id="run", session_id="session", user_id="user", mode="plan",
        owner_agent_id="child", agent_version_id="child-version", plan_node_id="plan-node",
        goal="Execute frozen node", status="running", active_runner_id="target-runner",
        execution_epoch=1, attempt_no=1, version=1,
    )
    caller_runner = DurableRunner(
        id="caller-runner", work_item_id="caller-work", run_id="run", session_id="session",
        agent_id="owner", agent_version_id="owner-version", execution_epoch=1, status="succeeded",
        principal_snapshot={}, assembly_snapshot={}, prompt_manifest={},
        input_snapshot={"plan_id": "plan", "plan_coordination_for_node_id": "plan-node"}, version=2,
    )
    caller_work = WorkItem(
        id="caller-work", run_id="run", session_id="session", user_id="user", mode="plan",
        owner_agent_id="owner", agent_version_id="owner-version", goal="Coordinate frozen node",
        status="waiting_agent", execution_epoch=1, attempt_no=1, version=2,
    )
    tool = ToolInvocation(
        id="tool", runner_id="caller-runner", work_item_id="caller-work", run_id="run",
        session_id="session", platform_tool_id="invoke-agent-tool", tool_key="invoke_agent",
        execution_mode="inline", status="waiting_resource", arguments={}, arguments_hash="a" * 64,
        business_idempotency_key="call", principal_hash="b" * 64, version=1,
    )
    invocation = AgentInvocation(
        id="invocation", caller_work_item_id="caller-work", caller_runner_id="caller-runner",
        target_work_item_id="target-work", target_agent_id="child",
        target_agent_version_id="child-version", mode="plan", request_text="Execute frozen node",
        context_snapshot={"tool_invocation_id": "tool", "plan_node_id": "plan-node"},
        status="running", batch_key="caller-runner", idempotency_key="call", version=1,
    )
    plan = SimpleNamespace(id="plan")
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[tool, plan, None, caller_work]),
        get=AsyncMock(return_value=caller_runner),
        flush=AsyncMock(),
    )
    event_writer = SimpleNamespace(
        append=AsyncMock(side_effect=[
            SimpleNamespace(id="child-settled"),
            SimpleNamespace(id="node-settled"),
            SimpleNamespace(id="coord-settled"),
        ]),
    )
    monkeypatch.setattr(worker_module, "ExecutionEventWriter", lambda _db: event_writer)
    worker = RuntimeWorker("plan-coordination-test")
    schedule = AsyncMock()
    monkeypatch.setattr(worker, "_schedule_ready_plan_nodes", schedule)

    await worker._settle_agent_target(
        db, target_runner, target_work, run, invocation, content="Child result",
    )

    assert caller_work.status == "succeeded"
    assert caller_work.execution_epoch == 1 and caller_work.active_runner_id is None
    assert caller_work.result_data == {"batch_key": "caller-runner", "plan_node_id": "plan-node"}
    assert run.lifecycle == "running" and run.wait_reason is None
    assert schedule.await_count == 2
