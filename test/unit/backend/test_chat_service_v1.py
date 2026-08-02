"""Durable Chat creation transaction tests."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    ExecutionEvent, ExecutionEventRecipient, OutboxCommand,
    PlatformAccount, UserIdentity, UserSession,
)
from app.models.runtime_v1 import DurableRunner, Message, Run, WorkItem
from app.services.chat_service import create_user_session, enqueue_chat_message

pytestmark = pytest.mark.unit


@pytest.fixture
async def chat_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, AgentBlueprint.__table__,
        AgentAssemblyVersion.__table__, UserSession.__table__, ExecutionEvent.__table__,
        ExecutionEventRecipient.__table__, Message.__table__, Run.__table__,
        WorkItem.__table__, DurableRunner.__table__, OutboxCommand.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, agent_id, version_id = uuid7(), uuid7(), uuid7()
    async with factory.begin() as db:
        db.add(UserIdentity(user_id="user-1", status="active"))
        db.add(PlatformAccount(id=account_id, user_id="user-1", display_name="User", status="active"))
        db.add(AgentBlueprint(
            id=agent_id, owner_account_id=account_id, visibility="enterprise",
            status="published", current_published_version_id=version_id,
            draft_version_id=uuid7(), next_version_no=2,
        ))
        db.add(AgentAssemblyVersion(
            id=version_id, agent_id=agent_id, version_no=1,
            publication_status="published", name="Agent", description="Does work",
            instructions="Work", group_enabled=False, created_by_account_id=account_id,
            immutable_at=utcnow(), published_at=utcnow(), content_hash="a" * 64,
        ))
    yield factory, agent_id, version_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_chat_command_creates_all_aggregates_event_and_outbox(chat_db):
    factory, agent_id, version_id = chat_db
    async with factory.begin() as db:
        session = await create_user_session(db, user_id="user-1", entry_agent_id=agent_id)
        message, run, work, runner = await enqueue_chat_message(
            db, session_id=session.id, user_id="user-1", content="Analyze this",
            idempotency_record_id="idem-1",
        )
        assert work.agent_version_id == version_id
        assert work.active_runner_id == runner.id
        assert message.run_id == run.id

    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Message)) == 1
        assert await db.scalar(select(func.count()).select_from(Run)) == 1
        assert await db.scalar(select(func.count()).select_from(WorkItem)) == 1
        assert await db.scalar(select(func.count()).select_from(DurableRunner)) == 1
        assert await db.scalar(select(func.count()).select_from(OutboxCommand)) == 1
        # session.created + message.created + run.created + runner.queued
        assert await db.scalar(select(func.count()).select_from(ExecutionEvent)) == 4
        stored = await db.get(UserSession, session.id)
        assert stored.next_event_sequence == 5


@pytest.mark.asyncio
async def test_empty_message_rolls_back_without_partial_run(chat_db):
    factory, agent_id, _ = chat_db
    async with factory.begin() as db:
        session = await create_user_session(db, user_id="user-1", entry_agent_id=agent_id)
    with pytest.raises(ValueError):
        async with factory.begin() as db:
            await enqueue_chat_message(
                db, session_id=session.id, user_id="user-1", content="  ",
                idempotency_record_id="idem-empty",
            )
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Run)) == 0
