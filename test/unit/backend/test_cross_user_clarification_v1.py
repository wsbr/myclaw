"""Cross-user person resolution and durable Clarification resume tests."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.clarifications import answer_clarification
from app.database import Base
from app.deps import SessionActor
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    ExecutionEvent, ExecutionEventRecipient, IdempotencyRecord, OutboxCommand,
    PlatformAccount, UserIdentity, UserSession,
)
from app.models.runtime_v1 import (
    Clarification, DurableRunner, Message, Run, RunnerCheckpoint, WorkItem,
)
from app.models.tenant_v1 import Tenant
from app.schemas.runtime_v1 import ClarificationAnswerRequest
from app.services.principal_ref_service import (
    InvalidPrincipalRef, decode_account_ref, encode_account_ref,
)


pytestmark = pytest.mark.unit


def test_platform_account_ref_is_signed_and_agent_tenant_bound():
    value = encode_account_ref(
        tenant_id="tenant-1", agent_id="agent-1", account_id="account-1",
    )
    assert decode_account_ref(
        value, tenant_id="tenant-1", agent_id="agent-1",
    ) == "account-1"
    with pytest.raises(InvalidPrincipalRef):
        decode_account_ref(value, tenant_id="tenant-2", agent_id="agent-1")
    with pytest.raises(InvalidPrincipalRef):
        decode_account_ref(value[:-1] + ("A" if value[-1] != "A" else "B"),
                           tenant_id="tenant-1", agent_id="agent-1")


@pytest.mark.asyncio
async def test_target_user_can_answer_without_owning_source_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Tenant.__table__, UserIdentity.__table__, PlatformAccount.__table__,
        AgentBlueprint.__table__, AgentAssemblyVersion.__table__,
        UserSession.__table__, ExecutionEvent.__table__,
        ExecutionEventRecipient.__table__, Message.__table__, Run.__table__,
        WorkItem.__table__, DurableRunner.__table__, RunnerCheckpoint.__table__,
        Clarification.__table__, OutboxCommand.__table__, IdempotencyRecord.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, requester_id, target_id = uuid7(), uuid7(), uuid7()
    agent_id, version_id = uuid7(), uuid7()
    session_id, run_id, work_id, runner_id, clarification_id = (
        uuid7() for _ in range(5)
    )
    async with factory.begin() as db:
        db.add(Tenant(
            id=tenant_id, slug="cross-user", name="Cross user", status="active",
        ))
        db.add_all([
            UserIdentity(user_id="requester", status="active"),
            UserIdentity(user_id="target", status="active"),
        ])
        await db.flush()
        requester = PlatformAccount(
            id=requester_id, tenant_id=tenant_id, user_id="requester",
            display_name="A", status="active",
        )
        target = PlatformAccount(
            id=target_id, tenant_id=tenant_id, user_id="target",
            display_name="B", status="active",
        )
        db.add_all([requester, target])
        await db.flush()
        db.add(AgentBlueprint(
            id=agent_id, tenant_id=tenant_id, owner_account_id=requester_id,
            visibility="enterprise", status="published",
            current_published_version_id=version_id, draft_version_id=uuid7(),
            next_version_no=2,
        ))
        db.add(AgentAssemblyVersion(
            id=version_id, agent_id=agent_id, version_no=1,
            publication_status="published", name="Reporter", description="D",
            instructions="I", group_enabled=False,
            created_by_account_id=requester_id, immutable_at=utcnow(),
            published_at=utcnow(), content_hash="a" * 64,
        ))
        db.add(UserSession(
            id=session_id, tenant_id=tenant_id, user_id="requester",
            account_id=requester_id, entry_agent_id=agent_id, status="active",
            origin_type="web", next_event_sequence=1,
        ))
        db.add(Run(
            id=run_id, tenant_id=tenant_id, account_id=requester_id,
            session_id=session_id, user_id="requester", mode="chat",
            status="waiting_user", wait_reason="waiting_user",
        ))
        db.add(WorkItem(
            id=work_id, tenant_id=tenant_id, run_id=run_id,
            session_id=session_id, user_id="requester", mode="chat",
            owner_agent_id=agent_id, agent_version_id=version_id, goal="work",
            status="waiting_user", execution_epoch=1, attempt_no=1,
        ))
        db.add(DurableRunner(
            id=runner_id, tenant_id=tenant_id, work_item_id=work_id,
            run_id=run_id, session_id=session_id, agent_id=agent_id,
            agent_version_id=version_id, execution_epoch=1, status="succeeded",
            principal_snapshot={
                "tenant_id": tenant_id, "account_id": requester_id,
                "user_id": "requester", "mode": "chat",
            },
            assembly_snapshot={"agent_version_id": version_id},
            prompt_manifest={}, input_snapshot={},
        ))
        db.add(Clarification(
            id=clarification_id, tenant_id=tenant_id,
            requester_account_id=requester_id, target_account_id=target_id,
            work_item_id=work_id, run_id=run_id, session_id=session_id,
            asked_by_agent_id=agent_id, target_user_id="target",
            target_display_name_snapshot="B", target_handle_snapshot="target",
            question="Which reporting period?", context_summary="Monthly report",
            status="open", idempotency_key="model-call-1",
        ))
        db.add(Message(
            id=uuid7(), session_id=session_id, user_id="requester",
            scope_type="chat", sender_type="agent", sender_agent_id=agent_id,
            recipient_type="platform_account", role="assistant",
            content="Which reporting period?", status="committed",
            run_id=run_id, work_item_id=work_id,
            clarification_id=clarification_id,
        ))

    actor = SessionActor(account=target, roles=frozenset({"user"}))
    async with factory() as db:
        response = await answer_clarification(
            clarification_id, ClarificationAnswerRequest(answer="Calendar month"),
            if_match='"1"', idempotency_key="target-answer-1", db=db, actor=actor,
        )
    assert response.status == "answered"
    assert response.answered_by_user_id == "target"

    async with factory() as db:
        answer = await db.scalar(select(Message).where(
            Message.clarification_id == clarification_id,
            Message.sender_type == "user",
        ))
        replacement = await db.scalar(select(DurableRunner).where(
            DurableRunner.work_item_id == work_id,
            DurableRunner.execution_epoch == 2,
        ))
    assert answer.sender_user_id == "target"
    assert answer.user_id == "requester"
    assert replacement.principal_snapshot["account_id"] == requester_id
    assert replacement.input_snapshot["pending_results"][-1]["content"].find(
        "Calendar month"
    ) >= 0
    await engine.dispose()
