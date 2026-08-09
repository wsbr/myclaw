"""Terminal Run settlement invokes its follow-up hook transactionally."""

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.common import utcnow, uuid7
from app.models.foundation import UserIdentity, UserSession
from app.models.runtime_v1 import DurableRunner, Run, WorkItem
from app.services.run_reliability_service import expire_one_run_deadline


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_deadline_failure_passes_its_terminal_event_to_followups(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session_id, run_id, work_id, runner_id = (uuid7() for _ in range(4))
    captured = {}

    async def capture_followups(db, *, run, runner, completion_event):
        captured.update({
            "run_id": run.id,
            "run_status": run.status,
            "runner_id": runner.id if runner else None,
            "event_type": completion_event.event_type,
            "event_id": completion_event.id,
        })

    monkeypatch.setattr(
        "app.services.run_reliability_service.enqueue_run_terminal_followups",
        capture_followups,
    )

    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="deadline-user", status="active"),
            UserSession(
                id=session_id, user_id="deadline-user", entry_agent_id="agent-1",
                status="active", origin_type="web", next_event_sequence=1,
            ),
            Run(
                id=run_id, session_id=session_id, user_id="deadline-user",
                mode="chat", status="active", root_work_item_id=work_id,
                deadline_at=utcnow() - timedelta(seconds=1),
            ),
            WorkItem(
                id=work_id, run_id=run_id, session_id=session_id,
                user_id="deadline-user", mode="chat", owner_agent_id="agent-1",
                agent_version_id="agent-version-1", goal="expire me",
                status="running", execution_epoch=1, attempt_no=1,
            ),
            DurableRunner(
                id=runner_id, work_item_id=work_id, run_id=run_id,
                session_id=session_id, agent_id="agent-1",
                agent_version_id="agent-version-1", execution_epoch=1,
                status="running", principal_snapshot={}, assembly_snapshot={},
                prompt_manifest={}, input_snapshot={},
            ),
        ])

    async with factory.begin() as db:
        assert await expire_one_run_deadline(db, actor_id="deadline-worker") is True

    async with factory() as db:
        run = await db.get(Run, run_id)
        work = await db.get(WorkItem, work_id)
        runner = await db.get(DurableRunner, runner_id)
        assert run.status == "failed"
        assert run.terminal_reason == "run_deadline_exceeded"
        assert work.status == "timed_out"
        assert runner.status == "cancelled"
    assert {
        key: captured[key]
        for key in ("run_id", "run_status", "runner_id", "event_type")
    } == {
        "run_id": run_id,
        "run_status": "failed",
        "runner_id": runner_id,
        "event_type": "run.failed",
    }
    assert captured["event_id"]
    await engine.dispose()
