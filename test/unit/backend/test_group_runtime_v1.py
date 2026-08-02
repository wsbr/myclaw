"""Group follow-up Run creation freezes a complete node/version map."""

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.groups import create_group_run
from app.database import Base
from app.deps import SessionActor
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    ExecutionEvent, ExecutionEventRecipient, IdempotencyRecord, OutboxCommand,
    PlatformAccount, UserIdentity, UserSession,
)
from app.models.group_v1 import (
    GroupAgentNode, GroupRun, GroupRunAgentVersion, GroupRunReference, GroupSession,
)
from app.models.runtime_v1 import DurableRunner, Message, Run, WorkItem
from app.schemas.group_v1 import GroupRunCreateRequest
from app.workers.runtime_worker_v1 import RuntimeWorker

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_create_followup_group_run_freezes_all_nodes_and_replays():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, AgentBlueprint.__table__,
        AgentAssemblyVersion.__table__, UserSession.__table__, IdempotencyRecord.__table__,
        ExecutionEvent.__table__, ExecutionEventRecipient.__table__, Message.__table__,
        Run.__table__, WorkItem.__table__, DurableRunner.__table__, OutboxCommand.__table__,
        GroupSession.__table__, GroupAgentNode.__table__, GroupRun.__table__,
        GroupRunAgentVersion.__table__, GroupRunReference.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, session_id, group_id = uuid7(), uuid7(), uuid7()
    entry_agent, other_agent, entry_version, other_version = (uuid7() for _ in range(4))
    entry_node, other_node = uuid7(), uuid7()
    async with factory.begin() as db:
        account = PlatformAccount(id=account_id, user_id="u1", display_name="U", status="active")
        db.add_all([
            UserIdentity(user_id="u1", status="active"), account,
            AgentBlueprint(id=entry_agent, owner_account_id=account_id, visibility="enterprise",
                status="published", current_published_version_id=entry_version,
                draft_version_id=uuid7(), next_version_no=2),
            AgentBlueprint(id=other_agent, owner_account_id=account_id, visibility="enterprise",
                status="inactive", current_published_version_id=other_version,
                draft_version_id=uuid7(), next_version_no=2),
            AgentAssemblyVersion(id=entry_version, agent_id=entry_agent, version_no=1,
                publication_status="published", name="Entry", description="D", instructions="I",
                group_enabled=True, created_by_account_id=account_id, immutable_at=utcnow(),
                published_at=utcnow(), content_hash="a" * 64),
            AgentAssemblyVersion(id=other_version, agent_id=other_agent, version_no=1,
                publication_status="published", name="Other", description="D", instructions="I",
                group_enabled=True, created_by_account_id=account_id, immutable_at=utcnow(),
                published_at=utcnow(), content_hash="b" * 64),
            UserSession(id=session_id, user_id="u1", entry_agent_id=entry_agent,
                status="active", origin_type="web", next_event_sequence=1),
            GroupSession(id=group_id, parent_session_id=session_id, user_id="u1",
                entry_agent_id=entry_agent, status="open", goal="Original goal",
                bootstrap_context="Shared context", confirmed_at=utcnow()),
            GroupAgentNode(id=entry_node, group_session_id=group_id, agent_id=entry_agent,
                source_participant_id=uuid7(), is_entry=True, initial_responsibility="Own work",
                availability="available"),
            GroupAgentNode(id=other_node, group_session_id=group_id, agent_id=other_agent,
                source_participant_id=uuid7(), is_entry=False, initial_responsibility="Support",
                availability="available"),
        ])
    actor = SessionActor(account=account, roles=frozenset({"user"}))
    body = GroupRunCreateRequest(content="Follow-up work")
    async with factory() as db:
        first = await create_group_run(group_id, body, idempotency_key="group-run-1", db=db, actor=actor)
    assert first["owner_node_id"] == entry_node
    assert first["run"]["lifecycle"] == "running"
    assert {row["group_node_id"] for row in first["agent_versions"]} == {entry_node, other_node}
    unavailable = next(row for row in first["agent_versions"] if row["group_node_id"] == other_node)
    assert unavailable["participation_status"] == "unavailable"
    assert unavailable["agent_version_id"] is None

    async with factory() as db:
        replay = await create_group_run(group_id, body, idempotency_key="group-run-1", db=db, actor=actor)
        assert replay["id"] == first["id"]
        assert await db.scalar(select(func.count()).select_from(GroupRun)) == 1
        assert await db.scalar(select(func.count()).select_from(GroupRunAgentVersion)) == 2
        assert await db.scalar(select(func.count()).select_from(WorkItem)) == 1
        command = await db.scalar(select(OutboxCommand))
        assert command.topic == "runtime.runner"

    async with factory() as db:
        with pytest.raises(HTTPException) as mismatch:
            await create_group_run(
                group_id, GroupRunCreateRequest(content="Different payload"),
                idempotency_key="group-run-1", db=db, actor=actor,
            )
        assert mismatch.value.status_code == 409
        assert mismatch.value.detail["code"] == "idempotency_mismatch"

    async with factory() as db:
        with pytest.raises(HTTPException) as missing_node:
            await create_group_run(
                group_id, GroupRunCreateRequest(content="Directed", recipient_node_id="missing"),
                idempotency_key="group-run-missing", db=db, actor=actor,
            )
        assert missing_node.value.status_code == 422
        assert missing_node.value.detail["code"] == "group_node_not_found"

    async with factory.begin() as db:
        entry = await db.get(AgentBlueprint, entry_agent)
        entry.status = "inactive"
    async with factory() as db:
        before_runs = await db.scalar(select(func.count()).select_from(GroupRun))
        with pytest.raises(HTTPException) as unavailable:
            await create_group_run(
                group_id, GroupRunCreateRequest(content="Unavailable owner"),
                idempotency_key="group-run-unavailable", db=db, actor=actor,
            )
        assert unavailable.value.status_code == 409
        assert unavailable.value.detail["code"] == "group_owner_unavailable"
        assert await db.scalar(select(func.count()).select_from(GroupRun)) == before_runs
    await engine.dispose()


@pytest.mark.asyncio
async def test_group_settlement_preserves_partial_failure_and_delivers_summary(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, AgentBlueprint.__table__,
        AgentAssemblyVersion.__table__, UserSession.__table__, ExecutionEvent.__table__,
        ExecutionEventRecipient.__table__, Message.__table__, Run.__table__, WorkItem.__table__,
        DurableRunner.__table__, OutboxCommand.__table__, GroupSession.__table__,
        GroupAgentNode.__table__, GroupRun.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = utcnow()
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="u1", status="active"),
            PlatformAccount(id="account", user_id="u1", display_name="U", status="active"),
            AgentBlueprint(id="agent", owner_account_id="account", visibility="enterprise",
                status="published", current_published_version_id="version",
                draft_version_id="draft", next_version_no=2),
            AgentAssemblyVersion(id="version", agent_id="agent", version_no=1,
                publication_status="published", name="A", description="", instructions="",
                group_enabled=True, created_by_account_id="account", immutable_at=now,
                published_at=now, content_hash="a" * 64),
            UserSession(id="session", user_id="u1", entry_agent_id="agent", status="active",
                origin_type="web", next_event_sequence=1),
            GroupSession(id="group", parent_session_id="session", user_id="u1",
                entry_agent_id="agent", status="open", confirmed_at=now),
            GroupAgentNode(id="node", group_session_id="group", agent_id="agent",
                source_participant_id="participant", is_entry=True,
                initial_responsibility="Owner", availability="available"),
            Run(id="run", session_id="session", user_id="u1", mode="group",
                execution_kind="normal", lifecycle="settling", entry_agent_id="agent",
                started_at=now),
            GroupRun(id="group-run", run_id="run", group_session_id="group",
                owner_node_id="node", goal="Deliver"),
            WorkItem(id="success-work", run_id="run", session_id="session", user_id="u1",
                mode="group", owner_agent_id="agent", agent_version_id="version",
                group_run_id="group-run", group_node_id="node", goal="Success",
                status="succeeded", execution_epoch=1, attempt_no=1),
            WorkItem(id="failed-work", run_id="run", session_id="session", user_id="u1",
                mode="group", owner_agent_id="agent", agent_version_id="version",
                group_run_id="group-run", group_node_id="node", goal="Failure",
                status="failed", execution_epoch=1, attempt_no=1,
                error_code="expected_failure"),
            WorkItem(id="summary-work", run_id="run", session_id="session", user_id="u1",
                mode="group", owner_agent_id="agent", agent_version_id="version",
                group_run_id="group-run", group_node_id="node", goal="Summary",
                status="running", active_runner_id="summary-runner", execution_epoch=1, attempt_no=1),
            DurableRunner(id="summary-runner", work_item_id="summary-work", run_id="run",
                session_id="session", agent_id="agent", agent_version_id="version",
                execution_epoch=1, status="running", lease_token="token",
                principal_snapshot={}, assembly_snapshot={}, prompt_manifest={},
                input_snapshot={"group_run_id": "group-run", "group_settlement": True}),
        ])

    worker = RuntimeWorker("settlement-worker")
    async def no_artifacts(*args, **kwargs):
        return None
    monkeypatch.setattr(worker, "_attach_run_artifacts", no_artifacts)
    async with factory.begin() as db:
        runner = await db.get(DurableRunner, "summary-runner")
        work = await db.get(WorkItem, "summary-work")
        run = await db.get(Run, "run")
        await worker._settle_group_summary(db, runner, work, run, "Partial delivery")

    async with factory() as db:
        run = await db.get(Run, "run")
        runner = await db.get(DurableRunner, "summary-runner")
        messages = list(await db.scalars(select(Message).where(Message.run_id == "run")))
        completed = list(await db.scalars(select(ExecutionEvent).where(
            ExecutionEvent.run_id == "run", ExecutionEvent.event_type == "run.completed",
        )))
        assert run.lifecycle == "completed" and run.outcome == "partially_succeeded"
        assert runner.status == "succeeded"
        assert {message.scope_type for message in messages} == {"chat", "group"}
        assert len(completed) == 1
        assert completed[0].payload["outcome"] == "partially_succeeded"
    await engine.dispose()
