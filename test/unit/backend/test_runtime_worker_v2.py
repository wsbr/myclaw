"""Lease, fencing, outbox acknowledgement, and recovery tests for v2 worker."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models.common import utcnow, uuid7
from app.models.foundation import OutboxCommand, UserSession
from app.models.group_v1 import (
    GroupMembership, GroupRun, GroupRunMember, GroupSession, GroupSummaryRequest,
)
from app.models.runtime_v1 import (
    AgentInvocation, Approval, DurableRunner, PlanUpdate, Run, RunnerCheckpoint,
    WorkItem, WorkingPlan,
)
from app.services.plan_mode_service import PlanSubjectChanged, assert_current_plan_subject
from app.services.working_plan_service import update_working_plan
from app.workers.runtime_worker import RunnerFenced, RuntimeWorker
from unit.backend.runtime_worker_testkit import create_runtime_world

pytestmark = pytest.mark.unit


async def _patch_factory(monkeypatch, factory):
    import app.workers.runtime_worker as module
    monkeypatch.setattr(module, "AsyncSessionLocal", factory)


@pytest.mark.asyncio
@pytest.mark.parametrize("mysql_code", [1205, 1213])
async def test_transient_settlement_conflict_retries_without_failing_business_run(
    monkeypatch, mysql_code,
):
    worker = RuntimeWorker("settlement-retry-worker")
    attempts = 0

    async def flaky(value):
        nonlocal attempts
        attempts += 1
        assert value == "settlement"
        if attempts < 3:
            raise OperationalError(
                "SELECT work_item FOR UPDATE", {},
                RuntimeError(mysql_code, "lock conflict"),
            )

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr("app.workers.runtime_worker.asyncio.sleep", no_delay)
    await worker._settle_with_retry(flaky, "settlement")
    assert attempts == 3


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
async def test_runner_outbox_delivery_budget_fails_once_without_execution(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    from app.config import settings

    async with world.factory.begin() as db:
        command = await db.scalar(select(OutboxCommand).where(
            OutboxCommand.aggregate_id == world.runner_id,
        ))
        command.attempt_count = settings.outbox_max_delivery_attempts

    worker = RuntimeWorker("delivery-budget-worker")
    assert await worker.claim_one() is None
    assert await worker.claim_one() is None
    async with world.factory() as db:
        command = await db.scalar(select(OutboxCommand).where(
            OutboxCommand.aggregate_id == world.runner_id,
        ))
        runner = await db.get(DurableRunner, world.runner_id)
        work = await db.get(WorkItem, world.work_id)
        run = await db.get(Run, world.run_id)
        assert command.status == "dead"
        assert command.last_error == "outbox_delivery_budget_exhausted"
        assert runner.status == "failed" and work.status == "failed"
        assert run.status == "failed"
        assert run.terminal_reason == "outbox_delivery_budget_exhausted"
    await world.engine.dispose()


@pytest.mark.asyncio
async def test_reporter_delivery_budget_projects_durable_replacement_action(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    from app.config import settings

    group_session_id, group_run_id, membership_id = uuid7(), uuid7(), uuid7()
    async with world.factory.begin() as db:
        command = await db.scalar(select(OutboxCommand).where(
            OutboxCommand.aggregate_id == world.runner_id,
        ))
        run = await db.get(Run, world.run_id)
        work = await db.get(WorkItem, world.work_id)
        runner = await db.get(DurableRunner, world.runner_id)
        command.attempt_count = settings.outbox_max_delivery_attempts
        db.add(GroupSession(
            id=group_session_id, parent_session_id=run.session_id, user_id=run.user_id,
            entry_agent_id=runner.agent_id, initiator_agent_id=runner.agent_id,
            status="open", goal="test", membership_policy="fixed_for_run",
        ))
        db.add(GroupMembership(
            id=membership_id, group_session_id=group_session_id,
            agent_id=runner.agent_id, status="joined", joined_at=utcnow(),
        ))
        db.add(GroupRun(
            id=group_run_id, run_id=run.id, group_session_id=group_session_id,
            goal="test", reporter_agent_id=runner.agent_id, summary_epoch=1,
        ))
        await db.flush()
        db.add(GroupRunMember(
            group_run_id=group_run_id, agent_id=runner.agent_id,
            membership_id=membership_id, agent_version_id=runner.agent_version_id,
            participation_status="available",
        ))
        db.add(GroupSummaryRequest(
            group_run_id=group_run_id, summary_epoch=1,
            reporter_work_item_id=work.id, status="queued",
        ))
        work.group_run_id = group_run_id
        work.purpose = "group_summary"
        runner.input_snapshot = {"summary_epoch": 1}

    assert await RuntimeWorker("reporter-delivery-budget-worker").claim_one() is None
    async with world.factory() as db:
        run = await db.get(Run, world.run_id)
        member = await db.get(GroupRunMember, (group_run_id, runner.agent_id))
        request = await db.scalar(select(GroupSummaryRequest).where(
            GroupSummaryRequest.group_run_id == group_run_id,
        ))
        assert run.status == "waiting_user" and run.wait_reason == "waiting_user"
        assert member.participation_status == "unavailable" and member.version == 2
        assert request.status == "failed" and request.version == 2
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


@pytest.mark.asyncio
async def test_final_settlement_appends_after_tool_result_checkpoint(monkeypatch):
    world = await create_runtime_world()
    await _patch_factory(monkeypatch, world.factory)
    worker = RuntimeWorker("settlement-worker")
    claim = await worker.claim_one()
    fence = await worker.acquire_runner(claim)
    assert fence is not None
    async with world.factory.begin() as db:
        db.add(RunnerCheckpoint(
            runner_id=world.runner_id, work_item_id=world.work_id,
            sequence_no=2, checkpoint_type="tool_results_persisted",
            state_snapshot={"pending_results": []}, execution_epoch=1,
            context_manifest={}, result_cursor={}, pending_refs={}, budget_snapshot={},
            last_completed_step="tool_results_persisted", safe_to_resume=True,
        ))
    context = type("Context", (), {"manifest": (), "compaction": None})()
    await worker._settle_completed(fence, "ordinary final", context)
    async with world.factory() as db:
        checkpoints = list(await db.scalars(select(RunnerCheckpoint).where(
            RunnerCheckpoint.runner_id == world.runner_id,
        ).order_by(RunnerCheckpoint.sequence_no)))
        assert [item.sequence_no for item in checkpoints] == [1, 2, 3]
        assert checkpoints[-1].checkpoint_type == "turn_completed"
    await world.engine.dispose()


@pytest.mark.asyncio
async def test_plan_proposal_waits_for_exact_revision_approval_without_new_work_item(monkeypatch):
    world = await create_runtime_world(content="Investigate and plan a safe rollout")
    await _patch_factory(monkeypatch, world.factory)
    worker = RuntimeWorker("plan-proposal-worker")
    async with world.factory.begin() as db:
        run = await db.get(Run, world.run_id)
        work = await db.get(WorkItem, world.work_id)
        runner = await db.get(DurableRunner, world.runner_id)
        session = await db.get(UserSession, run.session_id)
        session.interaction_mode = "plan"
        session.interaction_revision += 1
        session.permission_mode = "plan_readonly"
        session.permission_revision += 1
        run.mode = "plan"
        run.permission_mode = "plan_readonly"
        work.mode = "plan"
        runner.principal_snapshot = {
            **runner.principal_snapshot,
            "mode": "plan",
            "permission_revision": session.permission_revision,
        }
    claim = await worker.claim_one()
    fence = await worker.acquire_runner(claim)
    assert fence is not None
    async with world.factory.begin() as db:
        runner = await db.get(DurableRunner, world.runner_id)
        view = await update_working_plan(
            db, session_id=runner.session_id, run_id=runner.run_id,
            owner_agent_id=runner.agent_id, runner_id=runner.id,
            source_turn_id=None,
            arguments={
                "summary": "Validate then implement",
                "items": [
                    {"text": "Read current behavior", "status": "completed"},
                    {"text": "Implement guarded change", "status": "pending"},
                    {"text": "Run acceptance tests", "status": "pending"},
                ],
            },
        )
        assert view["revision"] == 1

    context = type("Context", (), {"manifest": (), "compaction": None})()
    await worker._settle_completed(fence, "Here is the proposed implementation plan.", context)
    async with world.factory() as db:
        run = await db.get(Run, world.run_id)
        work = await db.get(WorkItem, world.work_id)
        session = await db.get(UserSession, run.session_id)
        plan = await db.scalar(select(WorkingPlan).where(WorkingPlan.run_id == run.id))
        update = await db.scalar(select(PlanUpdate).where(
            PlanUpdate.working_plan_id == plan.id,
        ))
        approval = await db.scalar(select(Approval).where(
            Approval.kind == "plan_implementation",
        ))
        assert run.status == "waiting_approval" and run.lifecycle == "waiting"
        assert work.status == "waiting_approval" and work.active_runner_id is None
        assert session.permission_mode == "plan_readonly"
        assert await db.scalar(select(func.count()).select_from(WorkItem)) == 1
        assert approval.subject_version == plan.revision == update.revision == 1
        assert approval.subject_snapshot == {
            "session_id": session.id,
            "run_id": run.id,
            "working_plan_id": plan.id,
            "plan_revision": 1,
            "plan_content_hash": update.content_hash,
            "permission_mode": "plan_readonly",
            "permission_revision": session.permission_revision,
            "target_permission_mode": "direct",
        }
        assert len(approval.subject_hash) == 64
    async with world.factory.begin() as db:
        approval = await db.scalar(select(Approval).where(
            Approval.kind == "plan_implementation",
        ))
        session = await db.get(UserSession, approval.session_id)
        session.permission_revision += 1
        await db.flush()
        with pytest.raises(PlanSubjectChanged, match="permission revision changed"):
            await assert_current_plan_subject(db, approval=approval)
    await world.engine.dispose()


@pytest.mark.asyncio
async def test_failed_agent_invocation_resumes_project_manager_instead_of_failing_run(monkeypatch):
    world = await create_runtime_world(content="Ask a test specialist")
    await _patch_factory(monkeypatch, world.factory)
    worker = RuntimeWorker("agent-result-worker")
    target_work_id, target_runner_id, invocation_id = uuid7(), uuid7(), uuid7()
    async with world.factory.begin() as db:
        run = await db.get(Run, world.run_id)
        caller_work = await db.get(WorkItem, world.work_id)
        caller_runner = await db.get(DurableRunner, world.runner_id)
        caller_runner.status = "succeeded"
        caller_work.status = "waiting_agent"
        caller_work.wait_reason = "waiting_agent"
        caller_work.active_runner_id = None
        run.status = "waiting_agent"
        run.lifecycle = "waiting"
        run.wait_reason = "waiting_agent"
        target_work = WorkItem(
            id=target_work_id, run_id=run.id, session_id=run.session_id,
            user_id=run.user_id, mode=run.mode,
            owner_agent_id=caller_runner.agent_id,
            agent_version_id=caller_runner.agent_version_id,
            parent_work_item_id=caller_work.id, goal="Review tests",
            purpose="delegation", status="running", active_runner_id=target_runner_id,
            execution_epoch=1, attempt_no=1,
        )
        target_runner = DurableRunner(
            id=target_runner_id, work_item_id=target_work.id, run_id=run.id,
            session_id=run.session_id, agent_id=caller_runner.agent_id,
            agent_version_id=caller_runner.agent_version_id,
            execution_epoch=1, status="running", principal_snapshot={},
            assembly_snapshot={}, prompt_manifest={}, input_snapshot={},
        )
        invocation = AgentInvocation(
            id=invocation_id, run_id=run.id,
            caller_work_item_id=caller_work.id,
            caller_runner_id=caller_runner.id,
            caller_agent_id=caller_runner.agent_id,
            target_work_item_id=target_work.id,
            target_agent_id=caller_runner.agent_id,
            target_agent_version_id=caller_runner.agent_version_id,
            mode=run.mode, return_route="caller", request_text="Review tests",
            context_snapshot={"provider_call_id": "agent-call", "input": {}},
            status="running", batch_key=caller_runner.id,
            idempotency_key="agent-call", operation_key="agent-call",
            request_hash="a" * 64, authorization_snapshot={},
        )
        db.add_all([target_work, target_runner, invocation])
        await db.flush()
        await worker._settle_agent_target(
            db, runner=target_runner, work=target_work, run=run,
            invocation=invocation, error=RuntimeError("specialist unavailable"),
        )

    async with world.factory() as db:
        run = await db.get(Run, world.run_id)
        caller_work = await db.get(WorkItem, world.work_id)
        invocation = await db.get(AgentInvocation, invocation_id)
        runners = list(await db.scalars(select(DurableRunner).where(
            DurableRunner.work_item_id == caller_work.id,
        ).order_by(DurableRunner.execution_epoch)))
        assert invocation.status == "failed"
        assert run.status == "active" and run.lifecycle == "running"
        assert caller_work.status == "queued" and caller_work.execution_epoch == 2
        assert runners[-1].status == "queued"
        assert "agent_invocation_failed" in runners[-1].input_snapshot["pending_results"][-1]["content"]
    await world.engine.dispose()
