"""A Runner cannot publish a final response while its WorkingPlan is unfinished."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.common import uuid7
from app.models.foundation import ExecutionEvent, OutboxCommand, UserIdentity, UserSession
from app.models.runtime_v1 import (
    DurableRunner, Message, PlanUpdate, Run, WorkItem, WorkingPlan,
)
from app.workers.runtime_worker import RunnerFence, RuntimeWorker

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_unfinished_plan_queues_replacement_and_only_completed_revision_publishes(
    monkeypatch,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.workers.runtime_worker.AsyncSessionLocal", factory)

    session_id, run_id, work_id, runner_id, plan_id = (uuid7() for _ in range(5))
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="plan-user", status="active"),
            UserSession(
                id=session_id, user_id="plan-user", entry_agent_id="agent-1",
                status="active", origin_type="web", permission_mode="direct",
                next_event_sequence=1,
            ),
            Run(
                id=run_id, session_id=session_id, user_id="plan-user",
                mode="chat", status="active", root_work_item_id=work_id,
                hard_budget_snapshot={"max_tokens": 100},
            ),
            WorkItem(
                id=work_id, run_id=run_id, session_id=session_id,
                user_id="plan-user", mode="chat", owner_agent_id="agent-1",
                agent_version_id="agent-version-1", goal="finish two steps",
                status="running", active_runner_id=runner_id,
                execution_epoch=1, attempt_no=1,
            ),
            DurableRunner(
                id=runner_id, work_item_id=work_id, run_id=run_id,
                session_id=session_id, agent_id="agent-1",
                agent_version_id="agent-version-1", execution_epoch=1,
                fencing_token=1, status="running", lease_token="lease-1",
                principal_snapshot={}, assembly_snapshot={}, prompt_manifest={},
                input_snapshot={
                    "pending_results": [{
                        "role": "system",
                        "content": (
                            "[runtime:working_plan_completion_required]\nold notice"
                        ),
                    }],
                },
            ),
            WorkingPlan(
                id=plan_id, session_id=session_id, run_id=run_id,
                owner_agent_id="agent-1", summary="two steps", revision=1,
                status="active",
            ),
            PlanUpdate(
                id=uuid7(), working_plan_id=plan_id, revision=1,
                items=[
                    {"text": "first", "status": "completed"},
                    {"text": "second", "status": "in_progress"},
                ],
                updated_by_runner_id=runner_id, content_hash="a" * 64,
            ),
        ])

    worker = RuntimeWorker("plan-completion-test")
    context = SimpleNamespace(manifest=(), compaction=None)
    rejected_message_id = await worker._settle_completed(
        RunnerFence(
            runner_id=runner_id, work_item_id=work_id, run_id=run_id,
            session_id=session_id, execution_epoch=1, lease_token="lease-1",
        ),
        "premature partial answer", context,
    )
    assert rejected_message_id is None

    async with factory() as db:
        run = await db.get(Run, run_id)
        work = await db.get(WorkItem, work_id)
        source = await db.get(DurableRunner, runner_id)
        replacement = await db.get(DurableRunner, work.active_runner_id)
        messages = list(await db.scalars(select(Message).where(Message.run_id == run_id)))
        event = await db.scalar(select(ExecutionEvent).where(
            ExecutionEvent.event_type == "working_plan.completion_required",
        ))
        command = await db.scalar(select(OutboxCommand).where(
            OutboxCommand.aggregate_id == replacement.id,
        ))

        assert run.status == "active"
        assert work.status == "queued"
        assert work.execution_epoch == 2
        assert source.status == "succeeded"
        assert replacement.status == "queued"
        assert messages == []
        assert event.payload["incomplete_items"] == [
            {"text": "second", "status": "in_progress"},
        ]
        assert event.payload["rejected_message_hash"]
        notices = [
            item for item in replacement.input_snapshot["pending_results"]
            if item.get("role") == "system"
            and item.get("content", "").startswith(
                "[runtime:working_plan_completion_required]"
            )
        ]
        assert len(notices) == 1
        assert "second" in notices[0]["content"]
        assert command.status == "pending"
        replacement_id = replacement.id

    async with factory.begin() as db:
        work = await db.get(WorkItem, work_id)
        replacement = await db.get(DurableRunner, replacement_id)
        replacement.status = "running"
        replacement.lease_token = "lease-2"
        work.status = "running"
        plan = await db.get(WorkingPlan, plan_id)
        plan.revision = 2
        plan.version += 1
        db.add(PlanUpdate(
            id=uuid7(), working_plan_id=plan_id, revision=2,
            items=[
                {"text": "first", "status": "completed"},
                {"text": "second", "status": "completed"},
            ],
            updated_by_runner_id=replacement_id, content_hash="b" * 64,
        ))

    committed_message_id = await worker._settle_completed(
        RunnerFence(
            runner_id=replacement_id, work_item_id=work_id, run_id=run_id,
            session_id=session_id, execution_epoch=2, lease_token="lease-2",
        ),
        "verified final answer", context,
    )

    async with factory() as db:
        run = await db.get(Run, run_id)
        work = await db.get(WorkItem, work_id)
        plan = await db.get(WorkingPlan, plan_id)
        messages = list(await db.scalars(select(Message).where(Message.run_id == run_id)))
        assert committed_message_id == messages[0].id
        assert run.status == "completed"
        assert work.status == "succeeded"
        assert plan.status == "closed"
        assert [item.content for item in messages] == ["verified final answer"]

    await engine.dispose()
