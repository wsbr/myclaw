"""Outbox/Runner lease cut and Chat settlement test."""

import pytest
from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.model_hub.base import LLMResponse, TokenUsage
from app.database import Base
from app.models.agent_v1 import (
    AgentAssemblyVersion, AgentBlueprint, AgentVersionBinding, AgentVersionSkill, AgentVersionTool,
)
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    ExecutionEvent, ExecutionEventRecipient, OutboxCommand,
    PlatformAccount, UserIdentity, UserSession,
)
from app.models.registry import PlatformTool, PolicyDefinition, RegisteredModel, RegisteredSkill, SkillVersion
from app.models.runtime_v1 import (
    AgentInvocation, Clarification, DurableRunner, Message, Run, RunnerCheckpoint, ToolAttempt, ToolConfirmation,
    ToolInvocation, WorkItem,
    RunnerPolicySnapshot, RunnerSkillSnapshot, RunnerToolSnapshot,
)
from app.models.artifact_v1 import Artifact, MessageArtifact
from app.models.observability_v1 import ObservabilitySpan, ObservabilitySpanLink, RunnerTrace
from app.services.chat_service import create_user_session, enqueue_chat_message
from app.workers.runtime_worker_v1 import RuntimeWorker

pytestmark = pytest.mark.unit


class FakeLLM:
    async def chat(self, messages, tools, params):
        assert messages[-1]["content"] == "hello"
        return LLMResponse(
            content="completed response", tool_calls=[], usage=TokenUsage(),
            model="mock", finish_reason="stop",
        )


@pytest.mark.parametrize(
    ("request_text", "tool_name", "expected"),
    [
        ("You must call execute once with the supplied arguments.", "execute", True),
        ("Verification first.\nYou must call `search_group_history` exactly once.",
         "search_group_history", True),
        ("you MUST call 'invoke_agent' now!", "invoke_agent", True),
        ("Use execute only when necessary.", "execute", False),
        ("The Tool catalog contains search_group_history.", "search_group_history", False),
        ("You must call execute_extra.", "execute", False),
        ("Do not repeat the words You must call execute", "execute", False),
        ("", "execute", False),
    ],
)
def test_delegated_tool_authorization_requires_exact_capability_phrase(
    request_text, tool_name, expected,
):
    assert RuntimeWorker._delegated_tool_is_authorized(request_text, tool_name) is expected


@pytest.mark.parametrize(
    ("task_text", "tool_name", "expected"),
    [
        ("You must call execute once.", "execute", True),
        ("First call write_file, then call publish_artifact.", "write_file", True),
        ("First call write_file, then call publish_artifact.", "publish_artifact", True),
        ("Do not call any Tool.", "execute", False),
        ("Do not call execute.", "execute", False),
        ("Never call `execute` for this task.", "execute", False),
        ("slow runner restart acceptance", "execute", False),
        ("execute_extra is a different capability", "execute", False),
        ("SEARCH_GROUP_HISTORY is merely named", "search_group_history", False),
        ("Call SEARCH_GROUP_HISTORY for evidence", "search_group_history", True),
    ],
)
def test_top_level_tool_authorization_requires_exact_tool_key(
    task_text, tool_name, expected,
):
    assert RuntimeWorker._task_explicitly_names_tool(task_text, tool_name) is expected


class _ToolAuthorizationDB:
    def __init__(self, work):
        self.work = work

    async def get(self, model, key):
        return self.work if model is WorkItem and key == self.work.id else None

    async def scalar(self, _statement):
        return None


@pytest.mark.asyncio
async def test_resumed_chat_uses_immutable_work_goal_for_tool_capabilities():
    worker = RuntimeWorker("authorization-test")
    run = SimpleNamespace(execution_kind="normal", mode="chat")
    work = SimpleNamespace(
        id="work", parent_work_item_id=None, group_run_id=None, plan_node_id=None,
        goal=("Publish acceptance artifact: first call write_file, then call "
              "publish_artifact."),
    )
    resumed = SimpleNamespace(
        work_item_id=work.id,
        input_snapshot={"resumed_from_confirmation_id": "confirmation"},
        principal_snapshot={},
    )
    db = _ToolAuthorizationDB(work)

    assert await worker._runtime_tool_is_authorized(db, run, resumed, "write_file")
    assert await worker._runtime_tool_is_authorized(db, run, resumed, "publish_artifact")
    assert not await worker._runtime_tool_is_authorized(db, run, resumed, "execute")


@pytest.mark.asyncio
async def test_unrequested_chat_tool_is_not_authorized_after_runner_recovery():
    worker = RuntimeWorker("authorization-test")
    run = SimpleNamespace(execution_kind="normal", mode="chat")
    work = SimpleNamespace(
        id="work", parent_work_item_id=None, group_run_id=None, plan_node_id=None,
        goal="slow runner restart acceptance",
    )
    recovered = SimpleNamespace(
        work_item_id=work.id, input_snapshot={}, principal_snapshot={},
    )

    assert not await worker._runtime_tool_is_authorized(
        _ToolAuthorizationDB(work), run, recovered, "execute",
    )


@pytest.mark.asyncio
async def test_worker_claims_acknowledges_and_settles(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, RegisteredModel.__table__,
        PlatformTool.__table__, PolicyDefinition.__table__, RegisteredSkill.__table__,
        SkillVersion.__table__, AgentBlueprint.__table__, AgentAssemblyVersion.__table__,
        AgentVersionTool.__table__, AgentVersionSkill.__table__, AgentVersionBinding.__table__,
        UserSession.__table__, ExecutionEvent.__table__,
        ExecutionEventRecipient.__table__, Message.__table__, Run.__table__,
        WorkItem.__table__, DurableRunner.__table__, RunnerCheckpoint.__table__,
        ToolInvocation.__table__, ToolAttempt.__table__, ToolConfirmation.__table__, Clarification.__table__,
        AgentInvocation.__table__,
        RunnerSkillSnapshot.__table__, RunnerToolSnapshot.__table__, RunnerPolicySnapshot.__table__,
        OutboxCommand.__table__, Artifact.__table__, MessageArtifact.__table__,
        RunnerTrace.__table__, ObservabilitySpan.__table__, ObservabilitySpanLink.__table__,
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
            db, session_id=session.id, user_id="u1", content="hello",
            idempotency_record_id="idem",
        )

    import app.workers.runtime_worker_v1 as worker_module
    monkeypatch.setattr(worker_module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(worker_module.ModelHub, "get", lambda *args, **kwargs: FakeLLM())
    # A worker can die after claiming the Outbox command but before acquiring
    # the Runner.  A second worker reclaims the expired command, while the old
    # claim token is fenced and cannot advance the WorkItem.
    lost_worker = RuntimeWorker("lost-before-runner-acquire")
    stale_claim = await lost_worker.claim_one()
    assert stale_claim is not None
    async with factory.begin() as db:
        command = await db.get(OutboxCommand, stale_claim.command_id)
        command.dispatch_lease_expires_at = utcnow() - timedelta(seconds=1)
    worker = RuntimeWorker("replacement-worker")
    claim = await worker.claim_one()
    assert claim is not None and claim.command_id == stale_claim.command_id
    assert claim.claim_token != stale_claim.claim_token
    assert await lost_worker.acquire_runner(stale_claim) is None
    fence = await worker.acquire_runner(claim)
    assert fence is not None
    assert await worker.acknowledge(claim) is True
    await worker.execute(fence)

    async with factory() as db:
        stored_run = await db.get(Run, run.id)
        stored_work = await db.get(WorkItem, work.id)
        stored_runner = await db.get(DurableRunner, runner.id)
        command = await db.scalar(select(OutboxCommand))
        assert stored_run.lifecycle == "completed"
        assert stored_run.outcome == "succeeded", stored_runner.error_detail
        assert stored_work.status == "succeeded"
        assert stored_work.active_runner_id is None
        assert stored_runner.status == "succeeded"
        assert command.status == "completed"
        assert command.attempt_count == 2
        assert await db.scalar(select(func.count()).select_from(Message)) == 2
        trace = await db.scalar(select(RunnerTrace).where(RunnerTrace.runner_id == runner.id))
        assert trace is not None
        assert trace.status == "completed"
        spans = list(await db.scalars(select(ObservabilitySpan).where(
            ObservabilitySpan.runner_trace_id == trace.id,
        ).order_by(ObservabilitySpan.started_at, ObservabilitySpan.id)))
        assert {span.name for span in spans} == {"model_generation", "runner_execution"}
        assert all(span.status == "ok" for span in spans)
    await engine.dispose()
