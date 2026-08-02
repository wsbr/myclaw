"""Durable invoke_agent dispatch, wait, and caller resume tests."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint, AgentVersionBinding
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    ExecutionEvent, ExecutionEventRecipient, OutboxCommand, PlatformAccount, UserIdentity, UserSession,
)
from app.models.registry import PlatformTool, RegisteredModel
from app.models.runtime_v1 import (
    AgentInvocation, DurableRunner, Run, RunnerCheckpoint, ToolInvocation, WorkItem,
)
from app.services.agent_invocation_service import dispatch_agent_invocation
from app.workers.runtime_worker_v1 import RunnerFence, RuntimeWorker

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_agent_invocation_waits_and_resumes_the_caller():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, RegisteredModel.__table__, PlatformTool.__table__,
        AgentBlueprint.__table__, AgentAssemblyVersion.__table__, AgentVersionBinding.__table__,
        UserSession.__table__, ExecutionEvent.__table__, ExecutionEventRecipient.__table__,
        Run.__table__, WorkItem.__table__, DurableRunner.__table__, RunnerCheckpoint.__table__,
        ToolInvocation.__table__, AgentInvocation.__table__, OutboxCommand.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, model_id, tool_id = uuid7(), uuid7(), uuid7()
    owner_id, owner_version_id, child_id, child_version_id = uuid7(), uuid7(), uuid7(), uuid7()
    session_id, run_id, work_id, runner_id, tool_invocation_id = (uuid7() for _ in range(5))
    lease = uuid7()
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="u-invoke", status="active"),
            PlatformAccount(id=account_id, user_id="u-invoke", display_name="U", status="active"),
            RegisteredModel(id=model_id, key="invoke-model", provider="openai_compatible", model_name="mock",
                            default_parameters={}, capabilities={}, enabled=True, config_version=1),
            PlatformTool(id=tool_id, tool_key="invoke_agent", display_name="Invoke Agent", description="D",
                         input_schema={"type": "object"}, implementation_ref="builtin", implementation_version="1",
                         execution_mode="inline", enabled=True, confirmation_policy="never", platform_config={},
                         config_version=1, registration_hash="a" * 64),
        ])
        await db.flush()
        db.add_all([
            AgentBlueprint(id=owner_id, owner_account_id=account_id, visibility="enterprise", status="published",
                           current_published_version_id=owner_version_id, draft_version_id=uuid7(), next_version_no=2),
            AgentBlueprint(id=child_id, owner_account_id=account_id, visibility="enterprise", status="published",
                           current_published_version_id=child_version_id, draft_version_id=uuid7(), next_version_no=2),
        ])
        await db.flush()
        db.add_all([
            AgentAssemblyVersion(id=owner_version_id, agent_id=owner_id, version_no=1,
                                 publication_status="published", name="Owner", description="D", instructions="I",
                                 model_id=model_id, group_enabled=False, created_by_account_id=account_id,
                                 immutable_at=utcnow(), published_at=utcnow(), content_hash="a" * 64),
            AgentAssemblyVersion(id=child_version_id, agent_id=child_id, version_no=1,
                                 publication_status="published", name="Child", description="D", instructions="I",
                                 model_id=model_id, group_enabled=False, created_by_account_id=account_id,
                                 immutable_at=utcnow(), published_at=utcnow(), content_hash="b" * 64),
        ])
        await db.flush()
        db.add(AgentVersionBinding(agent_version_id=owner_version_id, bound_agent_id=child_id, sort_order=0))
        db.add(UserSession(id=session_id, user_id="u-invoke", entry_agent_id=owner_id,
                           status="active", origin_type="web"))
        run = Run(id=run_id, session_id=session_id, user_id="u-invoke", mode="chat", execution_kind="normal",
                  lifecycle="running", entry_agent_id=owner_id, started_at=utcnow())
        work = WorkItem(id=work_id, run_id=run_id, session_id=session_id, user_id="u-invoke", mode="chat",
                        owner_agent_id=owner_id, agent_version_id=owner_version_id, goal="Coordinate",
                        status="running", active_runner_id=runner_id, execution_epoch=1, attempt_no=1)
        runner = DurableRunner(id=runner_id, work_item_id=work_id, run_id=run_id, session_id=session_id,
                               agent_id=owner_id, agent_version_id=owner_version_id, execution_epoch=1,
                               status="running", lease_token=lease, lease_expires_at=utcnow(),
                               principal_snapshot={"user_id": "u-invoke", "session_id": session_id,
                                                   "entry_agent_id": owner_id, "current_agent_id": owner_id,
                                                   "mode": "chat", "delegation_chain": []},
                               assembly_snapshot={}, prompt_manifest={}, input_snapshot={})
        tool = ToolInvocation(id=tool_invocation_id, runner_id=runner_id, work_item_id=work_id, run_id=run_id,
                              session_id=session_id, platform_tool_id=tool_id, tool_key="invoke_agent",
                              execution_mode="inline", status="queued",
                              arguments={"agent_id": child_id, "task": "Analyze"}, arguments_hash="c" * 64,
                              business_idempotency_key="call-1", principal_hash="d" * 64, attempt_count=0)
        db.add_all([run, work, runner, tool])
        await db.flush()
        invocation = await dispatch_agent_invocation(
            db, tool_invocation=tool, caller_runner=runner, caller_work=work, run=run,
            arguments=tool.arguments,
        )
        target_work_id = invocation.target_work_item_id

    worker = RuntimeWorker("test-agent-worker")
    fence = RunnerFence(runner_id, work_id, run_id, session_id, 1, lease)
    async with factory.begin() as db:
        await worker._settle_waiting_for_agents(fence, db)
    async with factory.begin() as db:
        invocation = await db.scalar(select(AgentInvocation).where(AgentInvocation.target_work_item_id == target_work_id))
        target_work = await db.get(WorkItem, target_work_id)
        target_runner = await db.get(DurableRunner, target_work.active_runner_id)
        run = await db.get(Run, run_id)
        assert run.lifecycle == "waiting" and run.wait_reason == "waiting_agent"
        assert (await db.get(WorkItem, work_id)).status == "waiting_agent"
        await worker._settle_agent_target(db, target_runner, target_work, run, invocation, content="Child result")

    async with factory() as db:
        caller_work = await db.get(WorkItem, work_id)
        tool = await db.get(ToolInvocation, tool_invocation_id)
        invocation = await db.scalar(select(AgentInvocation).where(AgentInvocation.target_work_item_id == target_work_id))
        run = await db.get(Run, run_id)
        assert invocation.status == "succeeded"
        assert tool.status == "succeeded"
        assert caller_work.status == "queued" and caller_work.execution_epoch == 2
        assert caller_work.active_runner_id is not None
        assert run.lifecycle == "running" and run.wait_reason is None
    await engine.dispose()
