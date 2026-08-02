"""Durable Clarification answer, replay, and Work Item resume tests."""

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.clarifications import answer_clarification
from app.database import Base
from app.deps import CurrentAccount
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    ExecutionEvent, ExecutionEventRecipient, IdempotencyRecord, OutboxCommand,
    PlatformAccount, UserIdentity, UserSession,
)
from app.models.runtime_v1 import Clarification, DurableRunner, Message, Run, WorkItem
from app.schemas.runtime_v1 import ClarificationAnswerRequest
from app.workers.runtime_worker_v1 import RuntimeWorker

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_answer_clarification_resumes_same_work_item_and_replays():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, AgentBlueprint.__table__,
        AgentAssemblyVersion.__table__, UserSession.__table__, ExecutionEvent.__table__,
        ExecutionEventRecipient.__table__, Message.__table__, Run.__table__, WorkItem.__table__,
        DurableRunner.__table__, Clarification.__table__, OutboxCommand.__table__,
        IdempotencyRecord.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, agent_id, version_id = uuid7(), uuid7(), uuid7()
    session_id, run_id, work_id, runner_id, clarification_id = (uuid7() for _ in range(5))
    async with factory.begin() as db:
        account = PlatformAccount(id=account_id, user_id="u1", display_name="U", status="active")
        db.add_all([
            UserIdentity(user_id="u1", status="active"), account,
            AgentBlueprint(
                id=agent_id, owner_account_id=account_id, visibility="enterprise", status="published",
                current_published_version_id=version_id, draft_version_id=uuid7(), next_version_no=2,
            ),
            AgentAssemblyVersion(
                id=version_id, agent_id=agent_id, version_no=1, publication_status="published",
                name="A", description="D", instructions="I", group_enabled=False,
                created_by_account_id=account_id, immutable_at=utcnow(), published_at=utcnow(),
                content_hash="a" * 64,
            ),
            UserSession(
                id=session_id, user_id="u1", entry_agent_id=agent_id, status="active",
                origin_type="web", next_event_sequence=1,
            ),
            Run(
                id=run_id, session_id=session_id, user_id="u1", mode="chat",
                execution_kind="normal", lifecycle="waiting", wait_reason="clarification",
                entry_agent_id=agent_id, started_at=utcnow(),
            ),
            WorkItem(
                id=work_id, run_id=run_id, session_id=session_id, user_id="u1", mode="chat",
                owner_agent_id=agent_id, agent_version_id=version_id, goal="work",
                status="waiting_user", execution_epoch=1, attempt_no=1,
            ),
            DurableRunner(
                id=runner_id, work_item_id=work_id, run_id=run_id, session_id=session_id,
                agent_id=agent_id, agent_version_id=version_id, execution_epoch=1,
                status="succeeded", principal_snapshot={"user_id": "u1", "mode": "chat"},
                assembly_snapshot={"agent_version_id": version_id}, prompt_manifest={}, input_snapshot={},
            ),
            Clarification(
                id=clarification_id, work_item_id=work_id, run_id=run_id, session_id=session_id,
                asked_by_agent_id=agent_id, target_user_id="u1", question="Which region?",
                status="open", idempotency_key="model-call-1",
            ),
            Message(
                id=uuid7(), session_id=session_id, user_id="u1", scope_type="chat",
                sender_type="agent", sender_agent_id=agent_id, recipient_type="user",
                role="assistant", content="Which region?", status="committed",
                run_id=run_id, work_item_id=work_id, clarification_id=clarification_id,
            ),
        ])

    current = CurrentAccount(account=account, roles=frozenset({"user"}))
    async with factory() as db:
        first = await answer_clarification(
            clarification_id, ClarificationAnswerRequest(answer="Taiwan"),
            if_match='"1"', idempotency_key="answer-1", db=db, current=current,
        )
    assert first.status == "answered"
    assert first.answer_text == "Taiwan"

    async with factory() as db:
        replay = await answer_clarification(
            clarification_id, ClarificationAnswerRequest(answer="Taiwan"),
            if_match='"1"', idempotency_key="answer-1", db=db, current=current,
        )
        work = await db.get(WorkItem, work_id)
        run = await db.get(Run, run_id)
        command = await db.scalar(select(OutboxCommand))
        assert await db.scalar(select(func.count()).select_from(Message)) == 2
    assert replay.version == first.version
    assert work.status == "queued" and work.execution_epoch == 2
    assert run.lifecycle == "running" and run.wait_reason is None
    assert command.topic == "runtime.control" and command.action == "resume_work_item"

    async with factory() as db:
        with pytest.raises(HTTPException) as error:
            await answer_clarification(
                clarification_id, ClarificationAnswerRequest(answer="Japan"),
                if_match='"2"', idempotency_key="answer-2", db=db, current=current,
            )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "action_already_resolved"
    await engine.dispose()


def test_clarification_authority_by_mode():
    run = Run(mode="chat", execution_kind="normal")
    direct = DurableRunner(principal_snapshot={"delegation_chain": []}, input_snapshot={})
    delegated = DurableRunner(
        principal_snapshot={"delegation_chain": ["caller"]},
        input_snapshot={"agent_invocation_id": "inv"},
    )
    assert RuntimeWorker._can_request_clarification(run, direct) is True
    assert RuntimeWorker._can_request_clarification(run, direct, has_answered=True) is False
    assert RuntimeWorker._can_request_clarification(run, delegated) is False
    group = Run(mode="group", execution_kind="normal")
    assert RuntimeWorker._can_request_clarification(group, delegated) is True
