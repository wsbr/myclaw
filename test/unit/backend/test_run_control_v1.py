"""Run cancellation fencing and idempotency tests."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.runs import cancel_run
from app.database import Base
from app.deps import CurrentAccount
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    ExecutionEvent, ExecutionEventRecipient, IdempotencyRecord, OutboxCommand,
    PlatformAccount, UserIdentity, UserSession,
)
from app.models.registry import PlatformTool
from app.models.runtime_v1 import (
    AgentInvocation, Approval, Clarification, DurableRunner, Message, Run,
    ToolAttempt, ToolConfirmation, ToolInvocation, WorkItem, WorkingPlan,
)
from app.schemas.runtime_v1 import RunCancelRequest
from app.services.chat_service import create_user_session, enqueue_chat_message
from app.services.run_reliability_service import expire_one_run_deadline

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_cancel_run_fences_runner_and_replays_response():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, AgentBlueprint.__table__,
        AgentAssemblyVersion.__table__, UserSession.__table__, ExecutionEvent.__table__,
        ExecutionEventRecipient.__table__, Message.__table__, Run.__table__, WorkItem.__table__,
        DurableRunner.__table__, PlatformTool.__table__, ToolInvocation.__table__,
        ToolAttempt.__table__, ToolConfirmation.__table__, OutboxCommand.__table__,
        AgentInvocation.__table__,
        Clarification.__table__,
        Approval.__table__, WorkingPlan.__table__,
        IdempotencyRecord.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, agent_id, version_id = uuid7(), uuid7(), uuid7()
    async with factory.begin() as db:
        account = PlatformAccount(id=account_id, user_id="u1", display_name="U", status="active")
        db.add(UserIdentity(user_id="u1", status="active"))
        db.add(account)
        db.add(AgentBlueprint(
            id=agent_id, owner_account_id=account_id, visibility="enterprise", status="published",
            current_published_version_id=version_id, draft_version_id=uuid7(), next_version_no=2,
        ))
        db.add(AgentAssemblyVersion(
            id=version_id, agent_id=agent_id, version_no=1, publication_status="published",
            name="A", description="D", instructions="I", group_enabled=False,
            created_by_account_id=account_id, immutable_at=utcnow(), published_at=utcnow(),
            content_hash="a" * 64,
        ))
        session = await create_user_session(db, user_id="u1", entry_agent_id=agent_id)
        _, run, work, runner = await enqueue_chat_message(
            db, session_id=session.id, user_id="u1", content="work", idempotency_record_id="create",
        )
        tool_id, invocation_id, attempt_id = uuid7(), uuid7(), uuid7()
        db.add(PlatformTool(
            id=tool_id, tool_key="cancel-tool", display_name="Cancel tool",
            description="test", input_schema={}, implementation_ref="test",
            implementation_version="1", execution_mode="durable_async", enabled=True,
            confirmation_policy="never", side_effect="non_idempotent",
            platform_config={}, config_version=1, registration_hash="a" * 64,
        ))
        db.add(ToolInvocation(
            id=invocation_id, runner_id=runner.id, work_item_id=work.id,
            run_id=run.id, session_id=session.id, platform_tool_id=tool_id,
            tool_key="cancel-tool", execution_mode="durable_async", status="running",
            arguments={}, arguments_hash="b" * 64,
            business_idempotency_key="cancel-operation", principal_hash="c" * 64,
            attempt_count=1,
        ))
        await db.flush()
        db.add(ToolAttempt(
            id=attempt_id, tool_invocation_id=invocation_id, attempt_no=1,
            status="running", lease_token=uuid7(), request_snapshot={},
            side_effect_started=True, outcome_confirmed=False,
        ))
        await db.flush()
        invocation = await db.get(ToolInvocation, invocation_id)
        invocation.active_attempt_id = attempt_id

    current = CurrentAccount(account=account, roles=frozenset({"user"}))
    async with factory() as db:
        first = await cancel_run(
            run.id, RunCancelRequest(reason="stop"), if_match='"1"',
            idempotency_key="cancel-1", db=db, current=current,
        )
    assert first.status == "cancelled"
    assert first.cancellation_requested_at is not None

    async with factory() as db:
        replay = await cancel_run(
            run.id, RunCancelRequest(reason="stop"), if_match='"1"',
            idempotency_key="cancel-1", db=db, current=current,
        )
        stored_work = await db.get(WorkItem, work.id)
        stored_runner = await db.get(DurableRunner, runner.id)
        stored_invocation = await db.get(ToolInvocation, invocation_id)
        stored_attempt = await db.get(ToolAttempt, attempt_id)
        events = list(await db.scalars(select(ExecutionEvent).where(ExecutionEvent.run_id == run.id)))
    assert replay.version == first.version
    assert stored_work.status == "cancelled"
    assert stored_work.active_runner_id is None
    assert stored_runner.status == "cancelled"
    assert stored_invocation.status == "outcome_unknown"
    assert stored_attempt.status == "outcome_unknown"
    assert stored_attempt.outcome_confirmed is False
    assert [event.event_type for event in events].count("run.cancelled") == 1
    assert replay.terminal_reason == "cancelled_by_user"
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_deadline_fences_actions_and_preserves_unknown_tool_outcome():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session_id, run_id, work_id, runner_id, tool_id, invocation_id, attempt_id = (
        uuid7() for _ in range(7)
    )
    async with factory.begin() as db:
        db.add(UserSession(
            id=session_id, user_id="deadline-user", entry_agent_id=uuid7(),
            status="active", origin_type="web", next_event_sequence=1,
        ))
        db.add(Run(
            id=run_id, session_id=session_id, user_id="deadline-user", mode="chat",
            status="waiting_tool",
            wait_reason="waiting_tool", entry_agent_id=uuid7(), started_at=utcnow(),
            deadline_at=utcnow() - timedelta(seconds=1),
        ))
        db.add(WorkItem(
            id=work_id, run_id=run_id, session_id=session_id, user_id="deadline-user",
            mode="chat", owner_agent_id=uuid7(), agent_version_id=uuid7(), goal="deadline",
            status="waiting_tool", execution_epoch=1, attempt_no=1,
            active_runner_id=runner_id,
        ))
        db.add(DurableRunner(
            id=runner_id, work_item_id=work_id, run_id=run_id, session_id=session_id,
            agent_id=uuid7(), agent_version_id=uuid7(), execution_epoch=1,
            status="running", principal_snapshot={}, assembly_snapshot={},
            prompt_manifest={}, input_snapshot={}, lease_token=uuid7(),
            lease_expires_at=utcnow() + timedelta(seconds=30),
        ))
        db.add(PlatformTool(
            id=tool_id, tool_key="deadline-tool", display_name="Deadline tool",
            description="test", input_schema={}, implementation_ref="test",
            implementation_version="1", execution_mode="durable_async", enabled=True,
            confirmation_policy="never", side_effect="non_idempotent",
            platform_config={}, config_version=1, registration_hash="a" * 64,
        ))
        db.add(ToolInvocation(
            id=invocation_id, runner_id=runner_id, work_item_id=work_id,
            run_id=run_id, session_id=session_id, platform_tool_id=tool_id,
            tool_key="deadline-tool", execution_mode="durable_async", status="running",
            arguments={}, arguments_hash="b" * 64,
            business_idempotency_key="deadline-operation", principal_hash="c" * 64,
            attempt_count=1,
        ))
        db.add(ToolAttempt(
            id=attempt_id, tool_invocation_id=invocation_id, attempt_no=1,
            status="running", lease_token=uuid7(), request_snapshot={},
            side_effect_started=True, outcome_confirmed=False,
            lease_expires_at=utcnow() + timedelta(seconds=30),
        ))
        await db.flush()
        invocation = await db.get(ToolInvocation, invocation_id)
        invocation.active_attempt_id = attempt_id
    async with factory.begin() as db:
        assert await expire_one_run_deadline(db, actor_id="deadline-test") is True
    async with factory() as db:
        run = await db.get(Run, run_id)
        work = await db.get(WorkItem, work_id)
        runner = await db.get(DurableRunner, runner_id)
        invocation = await db.get(ToolInvocation, invocation_id)
        attempt = await db.get(ToolAttempt, attempt_id)
        events = list(await db.scalars(select(ExecutionEvent).where(
            ExecutionEvent.run_id == run_id,
        )))
        assert run.status == "failed" and run.terminal_reason == "run_deadline_exceeded"
        assert work.status == "timed_out" and runner.status == "cancelled"
        assert invocation.status == "outcome_unknown"
        assert attempt.status == "outcome_unknown" and attempt.outcome_confirmed is False
        assert [item.event_type for item in events].count("run.failed") == 1
    async with factory.begin() as db:
        assert await expire_one_run_deadline(db, actor_id="deadline-test") is False
    await engine.dispose()
