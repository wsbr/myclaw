"""Durable maintenance commands use leases, fencing, retry and terminal events."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.common import utcnow, uuid7
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.foundation import (
    ExecutionEvent, OutboxCommand, PlatformAccount, UserIdentity, UserSession,
)
from app.models.history_v1 import HistorySummary
from app.models.tenant_v1 import DEFAULT_TENANT_ID
from app.models.registry import (
    ModelVersion, PolicyDefinition, PolicyVersion, RegisteredModel,
)
from app.models.runtime_v1 import DurableRunner, Message, Run, WorkItem
from app.runtime.protocol import NormalizedTurnResult, StopReason, ToolCallItem, Usage
from app.services.maintenance_command_service import (
    build_memory_result, HistoryResult, MaintenanceClaim,
    MaintenanceCommandError, MaintenanceCommandWorker,
)

pytestmark = pytest.mark.unit


async def _fixture(factory):
    session_id, message_id, summary_id, command_id = (uuid7() for _ in range(4))
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="maintenance-user", status="active"),
            UserSession(
                id=session_id, user_id="maintenance-user", entry_agent_id="agent-1",
                status="active", next_event_sequence=1,
            ),
            Message(
                id=message_id, session_id=session_id, user_id="maintenance-user",
                scope_type="chat", sender_type="user",
                sender_user_id="maintenance-user", recipient_type="session",
                role="user", content="remember the verified outcome",
                status="committed",
            ),
            HistorySummary(
                id=summary_id, session_id=session_id, scope_type="chat",
                through_message_id=message_id, through_event_sequence=1,
                source_hash="a" * 64, status="pending", model_id="model-1",
                parameter_version=1,
            ),
            OutboxCommand(
                id=command_id, topic="runtime.maintenance",
                action="summarize_history", aggregate_type="history_summary",
                aggregate_id=summary_id, partition_key=session_id,
                dedupe_key=f"summary:{summary_id}",
                tenant_id=DEFAULT_TENANT_ID, account_id="maintenance-user",
                payload_ref={
                    "history_summary_id": summary_id,
                    "model_version_id": "model-version-1",
                },
                priority=100, available_at=utcnow(), status="pending",
            ),
        ])
    return session_id, summary_id, command_id


@pytest.mark.asyncio
async def test_history_command_success_records_actual_usage(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(
        "app.services.maintenance_command_service.AsyncSessionLocal", factory,
    )
    session_id, summary_id, command_id = await _fixture(factory)

    async def result(_payload):
        return HistoryResult(
            summary_id=summary_id, session_id=session_id,
            model_version_id="model-version-1", summary="bounded semantic summary",
            usage={"input_tokens": 23, "output_tokens": 7, "cached_input_tokens": 0},
        )

    monkeypatch.setattr(
        "app.services.maintenance_command_service.build_history_result", result,
    )
    worker = MaintenanceCommandWorker("maintenance-unit")
    assert await worker.run_once() is True

    async with factory() as db:
        command = await db.get(OutboxCommand, command_id)
        summary = await db.get(HistorySummary, summary_id)
        event = await db.scalar(select(ExecutionEvent).where(
            ExecutionEvent.event_type == "history.summary_ready",
        ))
        assert command.status == "completed"
        assert command.dispatch_lease_token is None
        assert summary.status == "ready"
        assert summary.summary == "bounded semantic summary"
        assert event.payload["model_version_id"] == "model-version-1"
        assert event.payload["usage"]["input_tokens"] == 23
    await engine.dispose()


@pytest.mark.asyncio
async def test_retry_dead_letter_and_stale_lease_fencing(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(
        "app.services.maintenance_command_service.AsyncSessionLocal", factory,
    )
    session_id, summary_id, command_id = await _fixture(factory)
    worker = MaintenanceCommandWorker("maintenance-unit")
    claim = await worker.claim_one()
    assert claim is not None
    assert await worker.heartbeat(claim) is True

    stale = MaintenanceClaim(
        command_id=claim.command_id, lease_token=uuid7(), action=claim.action,
        payload=claim.payload, attempt_count=claim.attempt_count,
    )
    await worker._settle(stale, HistoryResult(
        summary_id=summary_id, session_id=session_id,
        model_version_id="model-version-1", summary="must not settle", usage={},
    ), None)
    async with factory() as db:
        assert (await db.get(OutboxCommand, command_id)).status == "claimed"
        assert (await db.get(HistorySummary, summary_id)).status == "pending"

    retryable = MaintenanceCommandError(
        "temporary", code="provider_timeout", retryable=True,
    )
    await worker._settle(claim, None, retryable)
    async with factory.begin() as db:
        command = await db.get(OutboxCommand, command_id)
        assert command.status == "retry_wait"
        command.available_at = utcnow()
        command.attempt_count = settings.outbox_max_delivery_attempts - 1

    async def timeout(_payload):
        raise retryable

    monkeypatch.setattr(
        "app.services.maintenance_command_service.build_history_result", timeout,
    )
    assert await worker.run_once() is True
    async with factory() as db:
        command = await db.get(OutboxCommand, command_id)
        summary = await db.get(HistorySummary, summary_id)
        failed = await db.scalar(select(ExecutionEvent).where(
            ExecutionEvent.event_type == "history.summary_failed",
        ))
        assert command.status == "dead"
        assert command.last_error == "provider_timeout"
        assert summary.status == "failed"
        assert failed.payload["error_code"] == "provider_timeout"
    await engine.dispose()


@pytest.mark.asyncio
async def test_memory_extraction_uses_only_current_run_user_sources_and_frozen_snapshot(
    monkeypatch,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(
        "app.services.maintenance_command_service.AsyncSessionLocal", factory,
    )
    monkeypatch.setattr(
        "app.services.maintenance_command_service.resolve_provider_credential",
        lambda db, ref: _async_value("secret-value"),
    )
    account_id, agent_id, assembly_id = uuid7(), uuid7(), uuid7()
    model_id, model_version_id = uuid7(), uuid7()
    policy_id, policy_version_id = uuid7(), uuid7()
    session_id, run_id, work_id, runner_id = (uuid7() for _ in range(4))
    user_message_id = uuid7()
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="memory-user", status="active"),
            PlatformAccount(
                id=account_id, user_id="memory-user", display_name="Memory User",
                status="active",
            ),
            AgentBlueprint(
                id=agent_id, owner_account_id=account_id,
                visibility="enterprise", status="published",
            ),
            RegisteredModel(
                id=model_id, key=f"model-{model_id}", name="Model",
                provider="openai_compatible", model_name="model",
                default_parameters={}, capabilities={}, enabled=True,
            ),
            ModelVersion(
                id=model_version_id, model_id=model_id, version_no=1,
                provider="openai_compatible", model_name="model",
                credential_ref="secret://model",
                default_parameters={"extra_body": {"enable_thinking": True}},
                capabilities={}, content_hash="m" * 64,
            ),
            PolicyDefinition(
                id=policy_id, policy_key="memory_write.user_facts",
                policy_type="memory_write", name="Memory", description="Memory",
                implementation_ref="test", implementation_version="1",
                default_parameters={}, enabled=True, status="active",
            ),
            PolicyVersion(
                id=policy_version_id, policy_definition_id=policy_id,
                version_no=1, implementation_ref="test:v1", parameters={},
                content_hash="p" * 64, status="published",
            ),
            AgentAssemblyVersion(
                id=assembly_id, agent_id=agent_id, version_no=1,
                publication_status="published", name="Agent", description="Agent",
                instructions="Agent", model_id=model_id,
                model_version_id=model_version_id,
                memory_write_policy_id=policy_id,
                memory_write_policy_version_id=policy_version_id,
                group_enabled=False, created_by_account_id=account_id,
                content_hash="a" * 64,
            ),
            UserSession(
                id=session_id, user_id="memory-user", entry_agent_id=agent_id,
                status="active",
            ),
            Run(
                id=run_id, session_id=session_id, user_id="memory-user",
                mode="chat", status="completed", terminal_reason="completed",
                completed_at=utcnow(),
            ),
            WorkItem(
                id=work_id, run_id=run_id, session_id=session_id,
                user_id="memory-user", mode="chat", owner_agent_id=agent_id,
                agent_version_id=assembly_id, goal="remember", status="succeeded",
                execution_epoch=1, attempt_no=1,
            ),
            DurableRunner(
                id=runner_id, work_item_id=work_id, run_id=run_id,
                session_id=session_id, agent_id=agent_id,
                agent_version_id=assembly_id, execution_epoch=1,
                status="succeeded", principal_snapshot={}, assembly_snapshot={},
                prompt_manifest={}, input_snapshot={},
            ),
            Message(
                id=user_message_id, session_id=session_id, user_id="memory-user",
                scope_type="chat", sender_type="user",
                sender_user_id="memory-user", recipient_type="session", role="user",
                content="My durable codename is quartz-17.", status="committed",
                run_id=run_id, work_item_id=work_id,
            ),
            Message(
                id=uuid7(), session_id=session_id, user_id="memory-user",
                scope_type="chat", sender_type="agent", sender_agent_id=agent_id,
                recipient_type="session", role="assistant",
                content="Agent-authored false codename is hidden-99.",
                status="committed", run_id=run_id, work_item_id=work_id,
            ),
            Message(
                id=uuid7(), session_id=session_id, user_id="memory-user",
                scope_type="chat", sender_type="user",
                sender_user_id="memory-user", recipient_type="session", role="user",
                content="Old run secret must not be visible.", status="committed",
                run_id=uuid7(), work_item_id=work_id,
            ),
        ])

    captured = {}

    async def invoke(_version, _credential, *, messages, tools, parameters):
        captured["messages"] = messages
        captured["tools"] = tools
        captured["parameters"] = parameters
        return NormalizedTurnResult(
            items=(ToolCallItem(
                call_id="candidate-call", name="submit_memory_candidates",
                arguments={"candidates": [{
                    "fact_key": "acceptance_codename",
                    "content": "quartz-17", "confidence": 0.96,
                    "evidence_quote": "durable codename is quartz-17",
                    "source_refs": ["u1"],
                }, {
                    "fact_key": "low_confidence", "content": "ignore",
                    "confidence": 0.5, "evidence_quote": "quartz-17",
                    "source_refs": ["u1"],
                }]}, index=0,
            ),),
            stop_reason=StopReason.TOOL_USE,
            usage=Usage(input_tokens=31, output_tokens=11),
        )

    monkeypatch.setattr(
        "app.services.maintenance_command_service._invoke", invoke,
    )
    result = await build_memory_result({
        "run_id": run_id, "runner_id": runner_id,
        "policy_definition_id": policy_id,
        "policy_version_id": policy_version_id,
        "model_version_id": model_version_id,
    })

    prompt = str(captured["messages"])
    assert "quartz-17" in prompt
    assert "hidden-99" not in prompt
    assert "Old run secret" not in prompt
    assert captured["parameters"]["extra_body"]["enable_thinking"] is False
    assert captured["parameters"]["tool_choice"]["function"]["name"] == (
        "submit_memory_candidates"
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].source_message_ids == (user_message_id,)
    assert result.scope == "user"
    assert result.model_version_id == model_version_id
    assert result.usage["input_tokens"] == 31
    await engine.dispose()


async def _async_value(value):
    return value
