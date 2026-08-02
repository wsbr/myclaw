"""Run cancellation fencing and idempotency tests."""

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
    AgentInvocation, Clarification, DurableRunner, Message, Run, ToolAttempt, ToolConfirmation, ToolInvocation, WorkItem,
)
from app.schemas.runtime_v1 import RunCancelRequest
from app.services.chat_service import create_user_session, enqueue_chat_message

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

    current = CurrentAccount(account=account, roles=frozenset({"user"}))
    async with factory() as db:
        first = await cancel_run(
            run.id, RunCancelRequest(reason="stop"), if_match='"1"',
            idempotency_key="cancel-1", db=db, current=current,
        )
    assert first.lifecycle == "cancelled"
    assert first.cancellation_requested_at is not None

    async with factory() as db:
        replay = await cancel_run(
            run.id, RunCancelRequest(reason="stop"), if_match='"1"',
            idempotency_key="cancel-1", db=db, current=current,
        )
        stored_work = await db.get(WorkItem, work.id)
        stored_runner = await db.get(DurableRunner, runner.id)
        events = list(await db.scalars(select(ExecutionEvent).where(ExecutionEvent.run_id == run.id)))
    assert replay.version == first.version
    assert stored_work.status == "cancelled"
    assert stored_work.active_runner_id is None
    assert stored_runner.status == "cancelled"
    assert [event.event_type for event in events].count("run.cancelled") == 1
    await engine.dispose()
