"""MemoryHead arbitration keeps one active fact and gives later user edits priority."""

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.memories import MemoryPatch, delete_memory, edit_memory
from app.database import Base
from app.deps import CurrentAccount
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    ExecutionEvent, ExecutionEventRecipient, IdempotencyRecord, PlatformAccount,
    PlatformAuditEvent, UserIdentity, UserSession,
)
from app.models.memory_v1 import (
    MemoryCandidateSourceMessage, MemoryHead, MemoryRecord, MemorySourceMessage,
    MemoryWriteCandidate, RunnerMemorySnapshot,
)
from app.models.registry import PolicyDefinition
from app.models.runtime_v1 import DurableRunner, Message, Run, WorkItem
from app.services.memory_service import apply_memory_candidate, normalized_content, recall_memories

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_user_edit_wins_over_stale_model_candidate_and_delete_stops_recall():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, AgentBlueprint.__table__,
        AgentAssemblyVersion.__table__, PolicyDefinition.__table__, UserSession.__table__,
        Message.__table__, Run.__table__, WorkItem.__table__, DurableRunner.__table__,
        ExecutionEvent.__table__, ExecutionEventRecipient.__table__, IdempotencyRecord.__table__,
        PlatformAuditEvent.__table__, MemoryHead.__table__, MemoryRecord.__table__,
        MemorySourceMessage.__table__, RunnerMemorySnapshot.__table__,
        MemoryWriteCandidate.__table__, MemoryCandidateSourceMessage.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, agent_id, version_id, policy_id = (uuid7() for _ in range(4))
    session_id, run_id, work_id, runner_id, message_id = (uuid7() for _ in range(5))
    async with factory.begin() as db:
        account = PlatformAccount(id=account_id, user_id="u1", display_name="U", status="active")
        db.add_all([
            UserIdentity(user_id="u1", status="active"), account,
            AgentBlueprint(id=agent_id, owner_account_id=account_id, visibility="enterprise",
                status="published", current_published_version_id=version_id,
                draft_version_id=uuid7(), next_version_no=2),
            AgentAssemblyVersion(id=version_id, agent_id=agent_id, version_no=1,
                publication_status="published", name="A", description="D", instructions="I",
                group_enabled=False, created_by_account_id=account_id, immutable_at=utcnow(),
                published_at=utcnow(), content_hash="a" * 64),
            PolicyDefinition(id=policy_id, policy_key="memory_write.user_agent_facts",
                policy_type="memory_write", name="Write", description="D", implementation_ref="test",
                implementation_version="1", default_parameters={}, parameter_version=1, enabled=True),
            UserSession(id=session_id, user_id="u1", entry_agent_id=agent_id,
                status="active", origin_type="web", next_event_sequence=1),
            Run(id=run_id, session_id=session_id, user_id="u1", mode="chat",
                status="completed", terminal_reason="completed",
                entry_agent_id=agent_id, started_at=utcnow(), finished_at=utcnow(),
                completed_at=utcnow()),
            WorkItem(id=work_id, run_id=run_id, session_id=session_id, user_id="u1",
                mode="chat", owner_agent_id=agent_id, agent_version_id=version_id,
                goal="G", status="succeeded", execution_epoch=1, attempt_no=1),
            DurableRunner(id=runner_id, work_item_id=work_id, run_id=run_id,
                session_id=session_id, agent_id=agent_id, agent_version_id=version_id,
                execution_epoch=1, status="succeeded", principal_snapshot={}, assembly_snapshot={},
                prompt_manifest={}, input_snapshot={}),
            Message(id=message_id, session_id=session_id, user_id="u1", scope_type="chat",
                sender_type="user", sender_user_id="u1", recipient_type="session", role="user",
                content="Please remember that my preferred language is English.", status="committed",
                run_id=run_id, work_item_id=work_id),
        ])
    content, digest = normalized_content("English")
    first_id = uuid7()
    async with factory.begin() as db:
        db.add(MemoryWriteCandidate(id=first_id, user_id="u1", agent_id=agent_id,
            scope="user_agent", fact_key="preferred_language", content=content,
            normalized_hash=digest, source_session_id=session_id, runner_id=runner_id,
            policy_definition_id=policy_id, confidence=Decimal("0.95"), status="pending"))
        db.add(MemoryCandidateSourceMessage(candidate_id=first_id, message_id=message_id, source_role="user"))
    async with factory.begin() as db:
        accepted = await apply_memory_candidate(db, first_id)
        assert accepted.status == "accepted"
        initial = await db.get(MemoryRecord, accepted.result_memory_id)
        head = await db.get(MemoryHead, initial.head_id)
        initial_id, initial_head_version = initial.id, head.version

    # The candidate observes the old Head, then a user edit lands before the worker applies it.
    stale_id = uuid7(); _, stale_digest = normalized_content("French")
    async with factory.begin() as db:
        db.add(MemoryWriteCandidate(id=stale_id, user_id="u1", agent_id=agent_id,
            scope="user_agent", fact_key="preferred_language", content="French",
            normalized_hash=stale_digest, source_session_id=session_id, runner_id=runner_id,
            policy_definition_id=policy_id, observed_head_version=initial_head_version,
            confidence=Decimal("0.99"), status="pending"))
        db.add(MemoryCandidateSourceMessage(candidate_id=stale_id, message_id=message_id, source_role="user"))

    current = CurrentAccount(account=account, roles=frozenset({"user"}))
    async with factory() as db:
        edited = await edit_memory(initial_id, MemoryPatch(content="British English"),
            if_match=str(initial_head_version), idempotency_key="edit-1", db=db, current=current)
    assert edited["content"] == "British English"
    edited_id, edited_head_version = edited["id"], edited["head_version"]

    async with factory.begin() as db:
        rejected = await apply_memory_candidate(db, stale_id)
        assert rejected.status == "rejected"
        assert rejected.rejection_code == "user_edit_wins"
        active = await db.scalar(select(MemoryRecord).where(
            MemoryRecord.head_id == edited["head_id"], MemoryRecord.status == "active"))
        assert active.id == edited_id and active.content == "British English"

    async with factory() as db:
        response = await delete_memory(edited_id, if_match=str(edited_head_version),
            idempotency_key="delete-1", db=db, current=current)
        assert response.status_code == 204
    async with factory.begin() as db:
        recalled = await recall_memories(db, runner_id=uuid7(), user_id="u1",
            agent_id=agent_id, policy_key="memory_recall.user_combined")
        assert recalled == []
    await engine.dispose()
