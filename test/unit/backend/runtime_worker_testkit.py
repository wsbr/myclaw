from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.agent_v1 import (
    AgentAssemblyVersion, AgentBlueprint, AgentVersionBinding, AgentVersionSkill, AgentVersionTool,
)
from app.models.artifact_v1 import Artifact, MessageArtifact
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    ExecutionEvent, ExecutionEventRecipient, IdempotencyRecord, OutboxCommand, PlatformAccount,
    UserIdentity, UserSession,
)
from app.models.observability_v1 import ObservabilitySpan, ObservabilitySpanLink, RunnerTrace
from app.models.registry import (
    PlatformTool, PolicyDefinition, RegisteredModel, RegisteredSkill, SkillVersion,
)
from app.models.runtime_v1 import (
    AgentInvocation, Clarification, DurableRunner, Message, Run, RunnerCheckpoint,
    RunnerPolicySnapshot, RunnerSkillSnapshot, RunnerToolSnapshot,
    ToolAttempt, ToolConfirmation, ToolInvocation, WorkItem,
)
from app.services.chat_service import create_user_session, enqueue_chat_message


@dataclass
class RuntimeWorld:
    engine: object
    factory: object
    run_id: str
    work_id: str
    runner_id: str


async def create_runtime_world(*, content: str = "hello") -> RuntimeWorld:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, RegisteredModel.__table__,
        PlatformTool.__table__, PolicyDefinition.__table__, RegisteredSkill.__table__,
        SkillVersion.__table__, AgentBlueprint.__table__, AgentAssemblyVersion.__table__,
        AgentVersionTool.__table__, AgentVersionSkill.__table__, AgentVersionBinding.__table__,
        UserSession.__table__, ExecutionEvent.__table__, ExecutionEventRecipient.__table__,
        IdempotencyRecord.__table__,
        Message.__table__, Run.__table__, WorkItem.__table__, DurableRunner.__table__,
        RunnerCheckpoint.__table__, ToolInvocation.__table__, ToolAttempt.__table__,
        ToolConfirmation.__table__, AgentInvocation.__table__, Clarification.__table__,
        RunnerSkillSnapshot.__table__,
        RunnerToolSnapshot.__table__, RunnerPolicySnapshot.__table__, OutboxCommand.__table__,
        Artifact.__table__, MessageArtifact.__table__, RunnerTrace.__table__,
        ObservabilitySpan.__table__, ObservabilitySpanLink.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, model_id, agent_id, version_id = uuid7(), uuid7(), uuid7(), uuid7()
    async with factory.begin() as db:
        db.add(UserIdentity(user_id="u1", status="active"))
        db.add(PlatformAccount(id=account_id, user_id="u1", display_name="U", status="active"))
        db.add(RegisteredModel(
            id=model_id, key="mock", provider="openai_compatible", model_name="mock",
            default_parameters={}, capabilities={}, enabled=True, config_version=1,
        ))
        db.add(AgentBlueprint(
            id=agent_id, owner_account_id=account_id, visibility="enterprise",
            status="published", current_published_version_id=version_id,
            draft_version_id=uuid7(), next_version_no=2,
        ))
        db.add(AgentAssemblyVersion(
            id=version_id, agent_id=agent_id, version_no=1,
            publication_status="published", name="A", description="D", instructions="I",
            model_id=model_id, group_enabled=False, created_by_account_id=account_id,
            immutable_at=utcnow(), published_at=utcnow(), content_hash="a" * 64,
        ))
        session = await create_user_session(db, user_id="u1", entry_agent_id=agent_id)
        _, run, work, runner = await enqueue_chat_message(
            db, session_id=session.id, user_id="u1", content=content,
            idempotency_record_id=uuid7(),
        )
    return RuntimeWorld(engine, factory, run.id, work.id, runner.id)
