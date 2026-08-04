"""Outbox/Runner lease cut and Chat settlement test."""

import json
import pytest
from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.model_hub.base import LLMResponse, TokenUsage, ToolCall
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
from app.workers.runtime_worker_v1 import RuntimeWorker, _parse_completion_turn

pytestmark = pytest.mark.unit


def test_completion_turn_parser_accepts_only_closed_machine_effects():
    assert _parse_completion_turn(
        '{"message":"请确认周期","required_tool_keys":["request_clarification"],'
        '"reason_code":"clarification_required"}'
    ) == {
        "message": "请确认周期",
        "required_tool_keys": ["request_clarification"],
        "reason_code": "clarification_required",
    }
    with pytest.raises(ValueError, match="only message"):
        _parse_completion_turn(
            '{"message":"ok","required_tool_keys":[],"reason_code":"no_observable_effect",'
            '"answer":"fake"}'
        )
    with pytest.raises(ValueError, match="unknown completion effect"):
        _parse_completion_turn(
            '{"message":"ok","required_tool_keys":["shell_by_text"],'
            '"reason_code":"multiple_effects_required"}'
        )


def test_completion_contract_eligibility_uses_topology_not_task_phrases():
    run = SimpleNamespace(execution_kind="normal", mode="group")
    runner = SimpleNamespace(
        input_snapshot={
            "group_run_id": "group-run",
            "goal": "任意自然业务话术",
            "acceptance_criteria": "Respond to the user request",
        },
        principal_snapshot={},
    )
    assert RuntimeWorker._completion_contract_task(run, runner) == {
        "goal": "任意自然业务话术",
        "acceptance_criteria": "Respond to the user request",
    }
    runner.input_snapshot["agent_invocation_id"] = "delegation"
    assert RuntimeWorker._completion_contract_task(run, runner) is None


def test_only_hidden_completed_one_shot_effects_receive_finalization_repair():
    manifest = {
        "completion_contract": {
            "status": "enforced",
            "pending_tool_keys": [],
            "satisfied_tool_keys": ["search_group_history"],
        },
        "capabilities": {"effective_tool_keys": ["read_file"]},
    }
    repeated = [SimpleNamespace(name="search_group_history")]
    assert RuntimeWorker._classify_repeated_satisfied_effect_tool_keys(
        manifest, repeated,
    ) == ["search_group_history"]

    # Unknown tools, still-pending effects, and currently exposed calls must go
    # through the normal authorization path rather than this bounded repair.
    assert RuntimeWorker._classify_repeated_satisfied_effect_tool_keys(
        manifest, [SimpleNamespace(name="hallucinated_shell")],
    ) == []
    pending = {
        **manifest,
        "completion_contract": {
            **manifest["completion_contract"],
            "pending_tool_keys": ["search_group_history"],
        },
    }
    assert RuntimeWorker._classify_repeated_satisfied_effect_tool_keys(
        pending, repeated,
    ) == []
    exposed = {
        **manifest,
        "capabilities": {"effective_tool_keys": ["search_group_history"]},
    }
    assert RuntimeWorker._classify_repeated_satisfied_effect_tool_keys(
        exposed, repeated,
    ) == []


def test_single_declared_clarification_becomes_a_durable_control_call():
    finalize = ToolCall(
        id="finalize-1",
        name="finalize_response",
        arguments={
            "message": "本轮按第二季度还是六月单月统计？",
            "required_tool_keys": ["request_clarification"],
            "reason_code": "clarification_required",
        },
    )
    materialized = RuntimeWorker._declared_clarification_call(finalize)
    assert materialized is not None
    assert materialized.name == "request_clarification"
    assert materialized.arguments == {"question": "本轮按第二季度还是六月单月统计？"}

    plain = RuntimeWorker._declared_clarification_from_content(json.dumps(
        finalize.arguments, ensure_ascii=False,
    ))
    assert plain is not None
    assert plain.arguments == {"question": "本轮按第二季度还是六月单月统计？"}

    finalize.arguments["required_tool_keys"] = ["request_clarification", "execute"]
    finalize.arguments["reason_code"] = "multiple_effects_required"
    assert RuntimeWorker._declared_clarification_call(finalize) is None


def test_plain_completion_fallback_requires_a_settled_nonempty_effect_ledger():
    contract = {"status": "enforced"}
    settled_search = {
        "required_tool_keys": ["search_group_history"],
        "satisfied_tool_keys": ["search_group_history"],
        "pending_tool_keys": [],
        "unavailable_tool_keys": [],
    }
    assert RuntimeWorker._plain_completion_after_satisfied_effects_is_safe(
        contract, settled_search, "可追溯摘要",
    )
    assert not RuntimeWorker._plain_completion_after_satisfied_effects_is_safe(
        contract, {**settled_search, "required_tool_keys": [], "satisfied_tool_keys": []},
        "声称已经执行但没有账本",
    )
    assert not RuntimeWorker._plain_completion_after_satisfied_effects_is_safe(
        contract, {**settled_search, "pending_tool_keys": ["search_group_history"]},
        "尚未检索",
    )
    write_only = {
        **settled_search,
        "required_tool_keys": ["write_file"],
        "satisfied_tool_keys": ["write_file"],
    }
    assert not RuntimeWorker._plain_completion_after_satisfied_effects_is_safe(
        contract, write_only, "文件已写入",
    )
    assert RuntimeWorker._plain_completion_after_satisfied_effects_is_safe(
        contract,
        {
            **write_only,
            "required_tool_keys": ["write_file", "publish_artifact"],
            "satisfied_tool_keys": ["write_file", "publish_artifact"],
        },
        "文件已正式发布",
    )


class FakeLLM:
    async def chat(self, messages, tools, params):
        assert messages[-1]["content"] == "hello"
        return LLMResponse(
            content="completed response", tool_calls=[], usage=TokenUsage(),
            model="mock", finish_reason="stop",
        )


class _ToolAuthorizationDB:
    def __init__(self, work):
        self.work = work

    async def get(self, model, key):
        return self.work if model is WorkItem and key == self.work.id else None

    async def scalar(self, _statement):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("task_text", [
    "请根据订单数据核对退款差异，并给出证据。",
    "You must call execute exactly once.",
    "不要在回答中提到任何内部工具名称。",
    "",
])
async def test_chat_capability_is_independent_of_task_wording(task_text):
    worker = RuntimeWorker("authorization-test")
    run = SimpleNamespace(execution_kind="normal", mode="chat")
    work = SimpleNamespace(
        id="work", parent_work_item_id=None, group_run_id=None, plan_node_id=None,
        goal=task_text,
    )
    runner = SimpleNamespace(
        work_item_id=work.id, input_snapshot={"content": task_text}, principal_snapshot={},
    )
    assert await worker._runtime_tool_is_authorized(
        _ToolAuthorizationDB(work), run, runner, "execute",
    )


@pytest.mark.asyncio
async def test_chat_and_plan_delegated_children_cannot_fan_out():
    worker = RuntimeWorker("authorization-test")
    work = SimpleNamespace(
        id="work", parent_work_item_id="parent", group_run_id=None, plan_node_id=None,
        goal="核对退款单并生成审计证据",
    )
    runner = SimpleNamespace(
        work_item_id=work.id,
        input_snapshot={"agent_invocation_id": "invocation", "request_text": work.goal},
        principal_snapshot={"delegation_chain": ["entry"]},
    )
    db = _ToolAuthorizationDB(work)

    for mode in ("chat", "plan"):
        run = SimpleNamespace(execution_kind="normal", mode=mode)
        assert await worker._runtime_tool_is_authorized(db, run, runner, "read_file")
        assert not await worker._runtime_tool_is_authorized(db, run, runner, "invoke_agent")
        assert not await worker._runtime_tool_is_authorized(
            db, run, runner, "request_clarification",
        )


@pytest.mark.asyncio
async def test_group_delegation_retains_graph_governed_collaboration_tools():
    worker = RuntimeWorker("authorization-test")
    run = SimpleNamespace(execution_kind="normal", mode="group")
    work = SimpleNamespace(
        id="work", parent_work_item_id="parent", group_run_id="group-run", plan_node_id=None,
        goal="请风控同事复核异常退款，并在口径不清时询问业务方。",
    )
    runner = SimpleNamespace(
        work_item_id=work.id,
        input_snapshot={"agent_invocation_id": "invocation", "group_run_id": "group-run"},
        principal_snapshot={"delegation_chain": ["entry"]},
    )
    db = _ToolAuthorizationDB(work)

    assert await worker._runtime_tool_is_authorized(db, run, runner, "invoke_agent")
    assert await worker._runtime_tool_is_authorized(db, run, runner, "search_group_history")
    assert await worker._runtime_tool_is_authorized(db, run, runner, "request_clarification")


@pytest.mark.asyncio
async def test_execution_phase_narrows_capabilities_from_machine_state():
    worker = RuntimeWorker("authorization-test")
    work = SimpleNamespace(
        id="work", parent_work_item_id=None, group_run_id=None, plan_node_id=None, goal="自然语言目标",
    )
    db = _ToolAuthorizationDB(work)

    proposal = SimpleNamespace(
        work_item_id=work.id, input_snapshot={}, principal_snapshot={},
    )
    assert not await worker._runtime_tool_is_authorized(
        db, SimpleNamespace(execution_kind="proposal_generation", mode="plan"), proposal, "execute",
    )

    chat_run = SimpleNamespace(execution_kind="normal", mode="chat")
    assert not await worker._runtime_tool_is_authorized(
        db, chat_run, proposal, "search_group_history",
    )

    settlement = SimpleNamespace(
        work_item_id=work.id, input_snapshot={"plan_settlement": True}, principal_snapshot={},
    )
    assert not await worker._runtime_tool_is_authorized(
        db, SimpleNamespace(execution_kind="normal", mode="plan"), settlement, "execute",
    )

    coordination = SimpleNamespace(
        work_item_id=work.id,
        input_snapshot={"plan_coordination_for_node_id": "node"},
        principal_snapshot={},
    )
    plan_run = SimpleNamespace(execution_kind="normal", mode="plan")
    assert await worker._runtime_tool_is_authorized(db, plan_run, coordination, "invoke_agent")
    assert not await worker._runtime_tool_is_authorized(db, plan_run, coordination, "execute")


@pytest.mark.asyncio
async def test_answered_clarification_is_not_exposed_again():
    worker = RuntimeWorker("authorization-test")
    run = SimpleNamespace(execution_kind="normal", mode="group")
    work = SimpleNamespace(
        id="work", parent_work_item_id=None, group_run_id="group-run", plan_node_id=None, goal="补齐口径",
    )
    runner = SimpleNamespace(
        work_item_id=work.id, input_snapshot={"group_run_id": "group-run"}, principal_snapshot={},
    )
    assert not await worker._runtime_tool_is_authorized(
        _ToolAuthorizationDB(work), run, runner, "request_clarification",
        has_answered_clarification=True,
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
