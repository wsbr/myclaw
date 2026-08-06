"""Conformance tests for the Agent-native provider protocol and unified loop."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.model_hub.base import BaseLLM, LLMChunk, LLMResponse, TokenUsage, ToolCall
from app.core.tool_registry.base import RuntimeContext, ToolResult
from app.database import Base
from app.models.foundation import UserSession
from app.models.runtime_v1 import AgentTurn, Approval, ProviderAttempt, ToolInvocation
from app.models.registry import ModelVersion
from app.runtime.action_dispatcher import ActionDispatcher
from app.runtime.agent_loop_coordinator import AgentLoopCoordinator, AgentLoopError, LoopIdentity
from app.runtime.context_assembler import ContextAssembler
from app.runtime.model_adapter import ModelAdapter
from app.runtime.protocol import ProviderRequest, ToolCallItem
from app.services.provider_conformance import ProviderConformanceSuite

pytestmark = pytest.mark.unit


TOOL_DEFINITION = {
    "name": "lookup",
    "description": "Lookup a value",
    "input_schema": {
        "type": "object",
        "properties": {"key": {"type": "string", "minLength": 1}},
        "required": ["key"],
        "additionalProperties": False,
    },
    "platform_tool_id": "tool-1",
    "registration_hash": "a" * 64,
    "execution_mode": "inline",
}


def _done(reason: str, *, input_tokens: int = 1, output_tokens: int = 1) -> LLMChunk:
    return LLMChunk(
        type="done", stop_reason=reason, model="scripted-model",
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class ScriptedProvider(BaseLLM):
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def chat(self, messages, tools, params) -> LLMResponse:
        raise NotImplementedError

    async def stream(self, messages, tools, params) -> AsyncIterator[LLMChunk]:
        self.requests.append({"messages": messages, "tools": tools, "params": params})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        for item in response:
            if isinstance(item, Exception):
                raise item
            yield item

    async def count_tokens(self, messages):
        return 1


def _context():
    return ContextAssembler().assemble(
        platform_rules="Use native Tool Calls only.",
        agent_instructions="Help the user.",
        current_input="find alpha",
    )


def _runtime_context():
    return RuntimeContext(
        session_id="session-1", runner_id="runner-1", task_id="work-1",
        user_id="user-1", agent_id="agent-1", account_id="account-1",
    )


def _conformance_responses(*, invalid_coordination: str | None = None):
    coordination = [
        ("update_plan", {"revision": 2}),
        ("ask_user", {"question": "Which region should be used?"}),
        ("invoke_agent", {
            "agent_id": "agent-conformance", "request": "Review the evidence.",
        }),
    ]
    responses = [
        [LLMChunk(type="text_delta", content="plain"), _done("stop")],
        [LLMChunk(type="tool_call_delta", tool_call=ToolCall(
            id="probe-call", name="myclaw_conformance_echo",
            arguments={"value": "native-v2"},
        )), _done("tool_calls")],
        [LLMChunk(type="text_delta", content="native-v2"), _done("stop")],
        [LLMChunk(type="tool_call_delta", tool_call=ToolCall(
            id="probe-call-repeat", name="myclaw_conformance_echo",
            arguments={"value": "native-v2-repeat"},
        )), _done("tool_calls")],
        [LLMChunk(type="text_delta", content="native-v2-repeat"), _done("stop")],
        [LLMChunk(type="tool_call_delta", tool_call=ToolCall(
            id="probe-call-repair", name="myclaw_conformance_echo",
            arguments={"value": "repaired"},
        )), _done("tool_calls")],
    ]
    for index, (name, arguments) in enumerate(coordination):
        if index == 0 and invalid_coordination == "plain_text":
            responses.append([LLMChunk(type="text_delta", content="not a tool"), _done("stop")])
            continue
        if index == 0 and invalid_coordination == "wrong_tool":
            name = "ask_user"
        if index == 0 and invalid_coordination == "bad_arguments":
            arguments = {"revision": "two"}
        responses.append([LLMChunk(type="tool_call_delta", tool_call=ToolCall(
            id=f"coordination-{index}", name=name, arguments=arguments,
        )), _done("tool_calls")])
    return responses


def _conformance_version():
    return ModelVersion(
        id="model-version-1", model_id="model-1", version_no=1,
        provider="scripted", model_name="scripted", default_parameters={},
        capabilities={
            "streaming": True, "native_tool_calling": True,
            "tool_result_continuation": True, "stop_reason": True,
        },
        content_hash="f" * 64,
    )


@pytest.mark.asyncio
async def test_native_tool_result_then_plain_final_and_tool_remains_visible():
    provider = ScriptedProvider([
        [
            LLMChunk(type="tool_call_delta", tool_call=ToolCall(
                id="call-1", name="lookup", arguments={"key": "alpha"},
            )),
            _done("tool_calls"),
        ],
        [LLMChunk(type="text_delta", content="Alpha is 42."), _done("stop")],
    ])
    executions = []

    async def execute(name, arguments, context, db):
        executions.append((name, arguments))
        return ToolResult(status="succeeded", summary="Found", data={"value": 42})

    dispatcher = ActionDispatcher([TOOL_DEFINITION], executor=execute)
    result = await AgentLoopCoordinator(ModelAdapter(provider), dispatcher).run(
        context=_context(), tool_definitions=[TOOL_DEFINITION],
        capability_snapshot={"id": "cap-1", "effective_tool_keys": ["lookup"]},
        runtime_context=_runtime_context(), model_parameters={"model_id": "scripted"},
    )

    assert result.status == "completed"
    assert result.final_message == "Alpha is 42."
    assert executions == [("lookup", {"key": "alpha"})]
    assert provider.requests[1]["messages"][-1]["role"] == "tool"
    assert provider.requests[0]["tools"] == provider.requests[1]["tools"]
    assert [tool["name"] for tool in provider.requests[1]["tools"]] == ["lookup"]


@pytest.mark.asyncio
async def test_normalized_turn_preserves_commentary_and_tool_item_order():
    provider = ScriptedProvider([[
        LLMChunk(type="text_delta", content="before", item_index=0),
        LLMChunk(type="tool_call_delta", item_index=1, tool_call=ToolCall(
            id="ordered-call", name="lookup", arguments={"key": "alpha"},
        )),
        LLMChunk(type="text_delta", content="after", item_index=2),
        _done("tool_calls"),
    ]])
    result = await ModelAdapter(provider).invoke(ProviderRequest(
        messages=({"role": "user", "content": "ordered"},),
        tools=(TOOL_DEFINITION,), parameters={},
    ))
    assert [item.type for item in result.items] == [
        "assistant_message", "tool_call", "assistant_message",
    ]
    assert [item.text for item in result.assistant_messages] == ["before", "after"]


@pytest.mark.asyncio
async def test_one_schema_repair_is_returned_as_tool_result_then_succeeds():
    provider = ScriptedProvider([
        [LLMChunk(type="tool_call_delta", tool_call=ToolCall(
            id="bad", name="lookup", arguments={},
        )), _done("tool_calls")],
        [LLMChunk(type="tool_call_delta", tool_call=ToolCall(
            id="fixed", name="lookup", arguments={"key": "alpha"},
        )), _done("tool_calls")],
        [LLMChunk(type="text_delta", content="Repaired."), _done("stop")],
    ])

    async def execute(name, arguments, context, db):
        return ToolResult(status="succeeded", summary="ok", data={"value": 1})

    result = await AgentLoopCoordinator(
        ModelAdapter(provider), ActionDispatcher([TOOL_DEFINITION], executor=execute),
    ).run(
        context=_context(), tool_definitions=[TOOL_DEFINITION],
        capability_snapshot={"id": "cap-1", "effective_tool_keys": ["lookup"]},
        runtime_context=_runtime_context(), model_parameters={},
    )
    assert result.final_message == "Repaired."
    assert result.dispatches[0].schema_error is True
    assert "tool_arguments_schema_error" in provider.requests[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_second_schema_error_exhausts_the_single_repair_budget():
    provider = ScriptedProvider([
        [LLMChunk(type="tool_call_delta", tool_call=ToolCall(
            id="bad-1", name="lookup", arguments={},
        )), _done("tool_calls")],
        [LLMChunk(type="tool_call_delta", tool_call=ToolCall(
            id="bad-2", name="lookup", arguments={"extra": True},
        )), _done("tool_calls")],
    ])
    coordinator = AgentLoopCoordinator(
        ModelAdapter(provider), ActionDispatcher([TOOL_DEFINITION]),
    )
    with pytest.raises(AgentLoopError, match="schema repair budget"):
        await coordinator.run(
            context=_context(), tool_definitions=[TOOL_DEFINITION],
            capability_snapshot={"id": "cap-1", "effective_tool_keys": ["lookup"]},
            runtime_context=_runtime_context(), model_parameters={},
        )


@pytest.mark.asyncio
async def test_zero_payload_retry_creates_two_provider_attempts_but_one_agent_turn():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(
            sync, tables=[AgentTurn.__table__, ProviderAttempt.__table__]
        ))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    provider = ScriptedProvider([
        RuntimeError("request timed out before response"),
        [LLMChunk(type="text_delta", content="Recovered."), _done("stop")],
    ])
    identity = LoopIdentity(
        runner_id="runner-1", work_item_id="work-1", run_id="run-1",
        agent_version_id="agent-version-1", model_version_id="model-version-1",
        provider="scripted",
    )
    async with factory.begin() as db:
        result = await AgentLoopCoordinator(
            ModelAdapter(provider), ActionDispatcher([]),
        ).run(
            context=_context(), tool_definitions=[],
            capability_snapshot={"id": "cap-1", "effective_tool_keys": []},
            runtime_context=_runtime_context(), model_parameters={}, db=db, identity=identity,
        )
        assert result.final_message == "Recovered."
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(AgentTurn)) == 1
        attempts = list(await db.scalars(select(ProviderAttempt).order_by(ProviderAttempt.attempt_no)))
        assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
        assert [attempt.received_payload for attempt in attempts] == [False, True]
    await engine.dispose()


@pytest.mark.asyncio
async def test_transport_error_after_payload_is_not_retried():
    provider = ScriptedProvider([[
        LLMChunk(type="text_delta", content="partial"), RuntimeError("request timed out"),
    ]])
    coordinator = AgentLoopCoordinator(ModelAdapter(provider), ActionDispatcher([]))
    with pytest.raises(AgentLoopError, match="Provider call failed"):
        await coordinator.run(
            context=_context(), tool_definitions=[],
            capability_snapshot={"id": "cap-1", "effective_tool_keys": []},
            runtime_context=_runtime_context(), model_parameters={},
        )
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_confirmation_policy_creates_durable_invocation_and_approval():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    definition = {**TOOL_DEFINITION, "confirmation_policy": "per_call"}
    async with factory.begin() as db:
        db.add(UserSession(
            id="session-1", user_id="user-1", entry_agent_id="agent-1",
            status="active", origin_type="web", next_event_sequence=1,
        ))
        await db.flush()
        result = await ActionDispatcher([definition]).dispatch(
            ToolCallItem(index=0, call_id="approval-call", name="lookup", arguments={"key": "alpha"}),
            capability_snapshot={"id": "cap-1", "effective_tool_keys": ["lookup"]},
            runtime_context=_runtime_context(), db=db, run_id="run-1",
            work_item_id="work-1", source_turn_id="turn-1",
        )
        assert result.durable_wait is True
        invocation = await db.get(ToolInvocation, result.invocation_id)
        approval = await db.scalar(select(Approval).where(
            Approval.subject_id == result.invocation_id,
        ))
        assert invocation.status == "waiting_confirmation"
        assert approval.status == "pending"
        assert approval.target_account_id == "account-1"
        assert approval.subject_hash == approval.request_hash
    await engine.dispose()


def test_context_compaction_preserves_current_input_and_pending_tool_result():
    package = ContextAssembler().assemble(
        platform_rules="rules", agent_instructions="instructions",
        current_input="current objective",
        history=[{"role": "user", "content": "old " * 5000}],
        pending_results=[{
            "role": "tool", "tool_use_id": "call-1", "content": "authoritative result",
        }],
        working_plan={"revision": 7, "steps": ["do work"]}, token_budget=700,
    )
    assert package.compaction is not None
    assert any(
        item.get("role") == "user" and item.get("content") == "current objective"
        for item in package.messages
    )
    assert package.messages[-1]["tool_use_id"] == "call-1"
    assert any("authoritative result" in item.get("content", "") for item in package.messages)
    assert package.compaction["working_plan_revision"] == 7


def test_context_compaction_never_drops_multiple_pending_results():
    pending = [{
        "role": "tool", "tool_use_id": f"call-{index}",
        "content": f"authoritative-{index} " + ("x" * 900),
    } for index in range(3)]
    package = ContextAssembler().assemble(
        platform_rules="rules", agent_instructions="instructions",
        current_input="continue", history=[{"role": "user", "content": "old " * 5000}],
        pending_results=pending, token_budget=600,
    )
    tool_results = [item for item in package.messages if item.get("role") == "tool"]
    assert [item["tool_use_id"] for item in tool_results] == ["call-0", "call-1", "call-2"]
    assert any(
        item.get("role") == "user" and item.get("content") == "continue"
        for item in package.messages
    )


def test_context_manifest_and_compaction_preserve_clarification_continuation():
    clarification = [{
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "clarification-call", "type": "tool_use", "name": "ask_user",
            "input": {"question": "Which reporting period?"},
        }],
    }, {
        "role": "tool", "tool_use_id": "clarification-call",
        "content": '{"status":"succeeded","data":{"answer":"2026 Q2"}}',
    }]
    package = ContextAssembler().assemble(
        platform_rules="rules", agent_instructions="instructions",
        current_input="finish the report",
        history=[{"role": "user", "content": "old " * 5000}],
        pending_results=clarification,
        working_plan={"revision": 3, "items": [{"text": "publish", "status": "in_progress"}]},
        token_budget=600,
    )
    assert package.compaction is not None
    assert package.compaction["working_plan_revision"] == 3
    assert any(item.get("source") == "working_plan" for item in package.manifest)
    assert sum(item.get("source") == "pending_result" for item in package.manifest) == 2
    assert package.messages[-2:] == tuple(clarification)


@pytest.mark.asyncio
async def test_target_provider_passes_the_agent_native_conformance_suite():
    provider = ScriptedProvider(_conformance_responses())
    report = await ProviderConformanceSuite(provider).run(_conformance_version())
    assert report.passed is True
    assert [probe["name"] for probe in report.probes] == [
        "declared_capabilities", "streaming_plain_message_stop_usage",
        "zero_payload_retry", "native_tool_call",
        "tool_result_to_plain_final_tool_still_visible",
        "repeated_same_tool_multi_round", "bad_arguments_schema_feedback_repair",
        "parallel_tool_calls", "payload_then_error_no_replay",
        "long_context_compaction", "tool_result_correlation",
        "runtime_coordination_tool_schemas",
    ]
    assert provider.requests[1]["tools"] == provider.requests[2]["tools"]
    coordination_requests = provider.requests[6:9]
    assert len(coordination_requests) == 3
    assert all(
        [tool["name"] for tool in request["tools"]]
        == ["update_plan", "ask_user", "invoke_agent"]
        for request in coordination_requests
    )
    coordination_probe = report.probes[-1]
    assert coordination_probe["passed"] is True
    assert [case["tool_name"] for case in coordination_probe["details"]["cases"]] == [
        "update_plan", "ask_user", "invoke_agent",
    ]
    assert len(coordination_probe["details"]["schema_bundle_hash"]) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["plain_text", "wrong_tool", "bad_arguments"])
async def test_coordination_schema_probe_cannot_pass_without_valid_native_calls(invalid):
    provider = ScriptedProvider(_conformance_responses(invalid_coordination=invalid))
    report = await ProviderConformanceSuite(provider).run(_conformance_version())
    probe = report.probes[-1]
    assert report.passed is False
    assert probe["name"] == "runtime_coordination_tool_schemas"
    assert probe["passed"] is False
    assert probe["details"]["cases"][0]["passed"] is False
