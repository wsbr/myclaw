import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.common import utcnow, uuid7
from app.core.tool_registry.base import ToolResult
from app.models.foundation import OutboxCommand, UserSession
from app.models.registry import PlatformTool
from app.models.runtime_v1 import DurableRunner, Run, ToolAttempt, ToolInvocation, WorkItem, Workspace
from app.models.verification_v1 import LateResultObservation
from app.workers.sandbox_worker import SandboxWorker

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_sandbox_worker_executes_attempt_and_resumes_work(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "sandbox_root", str(tmp_path))
    session_id, run_id, work_id, runner_id, invocation_id = (uuid7() for _ in range(5))
    workspace_root = tmp_path / session_id
    workspace_root.mkdir()
    tool_id = uuid7()
    async with factory.begin() as db:
        db.add(UserSession(
            id=session_id, user_id="u1", entry_agent_id=uuid7(), status="active",
            origin_type="web", next_event_sequence=1,
        ))
        db.add(Run(
            id=run_id, session_id=session_id, user_id="u1", mode="chat",
            execution_kind="normal", lifecycle="waiting", wait_reason="waiting_tool",
            entry_agent_id=uuid7(), started_at=utcnow(),
        ))
        db.add(WorkItem(
            id=work_id, run_id=run_id, session_id=session_id, user_id="u1", mode="chat",
            owner_agent_id=uuid7(), agent_version_id=uuid7(), goal="run echo",
            status="waiting_tool", execution_epoch=1, attempt_no=1,
        ))
        db.add(DurableRunner(
            id=runner_id, work_item_id=work_id, run_id=run_id, session_id=session_id,
            agent_id=uuid7(), agent_version_id=uuid7(), execution_epoch=1, status="succeeded",
            principal_snapshot={"user_id": "u1", "mode": "chat", "delegation_chain": []},
            assembly_snapshot={}, prompt_manifest={}, input_snapshot={"content": "run echo"},
        ))
        db.add(PlatformTool(
            id=tool_id, tool_key="execute", display_name="Execute", description="Execute argv",
            input_schema={}, output_schema=None, implementation_ref="builtin:execute",
            implementation_version="1", execution_mode="durable_async", enabled=True,
            confirmation_policy="always", platform_config={"workspace_access": "write"},
            config_version=1, registration_hash="a" * 64,
        ))
        db.add(ToolInvocation(
            id=invocation_id, runner_id=runner_id, work_item_id=work_id, run_id=run_id,
            session_id=session_id, platform_tool_id=tool_id, tool_key="execute",
            execution_mode="durable_async", status="queued",
            arguments={"argv": ["/bin/echo", "sandbox-ok"]}, arguments_hash="b" * 64,
            tool_registration_hash="a" * 64,
            business_idempotency_key="call-1", principal_hash="c" * 64, attempt_count=0,
        ))
        db.add(Workspace(
            id=uuid7(), session_sandbox_id=uuid7(), session_id=session_id,
            root_uri=f"workspace://{session_id}/", status="active",
            retention_policy_key="session_lifetime",
        ))
        db.add(OutboxCommand(
            id=uuid7(), topic="runtime.sandbox", action="execute_tool",
            aggregate_type="tool_invocation", aggregate_id=invocation_id,
            partition_key=session_id, dedupe_key="sandbox-call-1",
            payload_ref={"tool_invocation_id": invocation_id}, available_at=utcnow(), status="pending",
        ))

    import app.workers.sandbox_worker as worker_module
    monkeypatch.setattr(worker_module, "AsyncSessionLocal", factory)
    worker = SandboxWorker("sandbox-test")
    claim = await worker.claim_one()
    assert claim is not None
    await worker.execute(claim)

    async with factory() as db:
        invocation = await db.get(ToolInvocation, invocation_id)
        attempt = await db.scalar(select(ToolAttempt).where(ToolAttempt.tool_invocation_id == invocation_id))
        work = await db.get(WorkItem, work_id)
        replacement = await db.get(DurableRunner, work.active_runner_id)
        runner_command = await db.scalar(select(OutboxCommand).where(
            OutboxCommand.topic == "runtime.runner", OutboxCommand.aggregate_id == replacement.id,
        ))
        assert invocation.status == "succeeded"
        assert invocation.result_data["status"] == "succeeded"
        assert invocation.result_data["data"]["stdout"] == "sandbox-ok\n"
        assert attempt.status == "succeeded"
        assert work.status == "queued"
        assert replacement.status == "queued"
        assert runner_command.status == "pending"

    # A duplicated/late delivery is diagnostic only. It cannot overwrite the
    # settled invocation or revive a Run that became terminal meanwhile.
    async with factory.begin() as db:
        run = await db.get(Run, run_id)
        run.status = "cancelled"
        run.lifecycle = "cancelled"
        run.wait_reason = None
        run.finished_at = utcnow()
        run.completed_at = utcnow()
        terminal_version = run.version = run.version + 1
    await worker.settle(claim, ToolResult(
        status="succeeded", summary="late duplicate",
        data={"exit_code": 0, "stdout": "late\n", "stderr": ""},
    ))
    async with factory() as db:
        invocation = await db.get(ToolInvocation, invocation_id)
        run = await db.get(Run, run_id)
        observations = list(await db.scalars(select(LateResultObservation).where(
            LateResultObservation.source_id == claim.attempt_id,
        )))
        assert invocation.status == "succeeded"
        assert run.status == "cancelled" and run.version == terminal_version
        assert len(observations) == 1
    await engine.dispose()
