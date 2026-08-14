"""Provider stop reasons are enforced before any partial output is published."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.model_hub.base import BaseLLM, LLMChunk, LLMResponse, TokenUsage, ToolCall
from app.core.tool_registry.base import RuntimeContext
from app.database import Base
from app.runtime.action_dispatcher import ActionDispatcher
from app.runtime.agent_loop_coordinator import (
    AgentLoopCoordinator, AgentLoopError, LoopIdentity,
)
from app.runtime.context_assembler import ContextPackage
from app.runtime.model_adapter import ModelAdapter
from app.runtime.protocol import ProviderRequest

pytestmark = pytest.mark.unit


class _StoppedProvider(BaseLLM):
    def __init__(self, stop_reason: str, *, content: str = "partial", tool=False) -> None:
        self.stop_reason = stop_reason
        self.content = content
        self.tool = tool

    async def chat(self, messages, tools, params):
        return LLMResponse(
            content=self.content, tool_calls=[], usage=TokenUsage(), model="test",
            finish_reason=self.stop_reason,
        )

    async def stream(self, messages, tools, params):
        if self.content:
            yield LLMChunk(type="text_delta", content=self.content)
        if self.tool:
            yield LLMChunk(
                type="tool_call_delta", item_index=1,
                tool_call=ToolCall(id="call-1", name="noop", arguments={}),
            )
        yield LLMChunk(
            type="done", stop_reason=self.stop_reason, model="test",
            usage=TokenUsage(input_tokens=7, output_tokens=3),
        )

    async def count_tokens(self, messages, tools=None, params=None):
        return 1


class _TransactionProbeProvider(_StoppedProvider):
    def __init__(self, db) -> None:
        super().__init__("stop", content="complete")
        self.db = db
        self.transaction_states = []

    async def count_tokens(self, messages, tools=None, params=None):
        self.transaction_states.append(("count", self.db.in_transaction()))
        return 1

    async def stream(self, messages, tools, params):
        self.transaction_states.append(("stream", self.db.in_transaction()))
        async for chunk in super().stream(messages, tools, params):
            yield chunk


def _context() -> ContextPackage:
    return ContextPackage(
        messages=({"role": "user", "content": "go"},),
        manifest=(), estimated_tokens=1,
    )


def _runtime_context() -> RuntimeContext:
    return RuntimeContext(
        tenant_id="test-tenant",
        session_id="session", runner_id="runner", user_id="user", agent_id="agent",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("stop_reason", "expected_code"), [
    ("length", "provider_output_truncated"),
    ("content_filter", "provider_content_filtered"),
    ("error", "provider_invalid_stop_reason"),
    ("tool_calls", "provider_invalid_stop_reason"),
])
async def test_incomplete_provider_stop_never_returns_partial_answer(
    stop_reason, expected_code,
):
    coordinator = AgentLoopCoordinator(
        ModelAdapter(_StoppedProvider(stop_reason)), ActionDispatcher([]),
    )

    with pytest.raises(AgentLoopError) as raised:
        await coordinator.run(
            context=_context(), tool_definitions=[],
            capability_snapshot={"id": "snapshot", "effective_tool_keys": []},
            runtime_context=_runtime_context(), model_parameters={},
        )

    assert raised.value.code == expected_code


@pytest.mark.asyncio
async def test_truncated_tool_payload_is_not_dispatched():
    coordinator = AgentLoopCoordinator(
        ModelAdapter(_StoppedProvider("length", content="", tool=True)),
        ActionDispatcher([]),
    )

    with pytest.raises(AgentLoopError) as raised:
        await coordinator.run(
            context=_context(), tool_definitions=[],
            capability_snapshot={"id": "snapshot", "effective_tool_keys": []},
            runtime_context=_runtime_context(), model_parameters={},
        )

    assert raised.value.code == "provider_output_truncated"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["stop", None])
async def test_completed_or_provider_compatible_unknown_message_is_accepted(stop_reason):
    coordinator = AgentLoopCoordinator(
        ModelAdapter(_StoppedProvider(stop_reason, content="complete")),
        ActionDispatcher([]),
    )

    result = await coordinator.run(
        context=_context(), tool_definitions=[],
        capability_snapshot={"id": "snapshot", "effective_tool_keys": []},
        runtime_context=_runtime_context(), model_parameters={},
    )

    assert result.status == "completed"
    assert result.final_message == "complete"


@pytest.mark.asyncio
async def test_model_adapter_streams_only_normalized_text_events():
    events = []

    async def observe(event):
        events.append(event)

    await ModelAdapter(_StoppedProvider("stop", content="complete")).invoke(
        request=ProviderRequest(
            messages=({"role": "user", "content": "go"},),
            tools=(), parameters={},
        ),
        stream_observer=observe,
    )

    assert events[0].type == "text_delta"
    assert events[0].data == {"content": "complete"}
    assert events[-1].type == "done"


@pytest.mark.asyncio
async def test_persisted_agent_loop_wires_text_and_reset_to_turn_observer():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    observed = []

    async def observe(turn_id, event):
        observed.append((turn_id, event))

    coordinator = AgentLoopCoordinator(
        ModelAdapter(_StoppedProvider("length", content="partial")),
        ActionDispatcher([]), stream_observer=observe,
    )
    async with factory() as db:
        with pytest.raises(AgentLoopError):
            await coordinator.run(
                context=_context(), tool_definitions=[],
                capability_snapshot={"id": "snapshot", "effective_tool_keys": []},
                runtime_context=_runtime_context(), model_parameters={}, db=db,
                identity=LoopIdentity(
                    runner_id="runner", work_item_id="work", run_id="run",
                    agent_version_id="agent-version", model_version_id="model-version",
                    provider="test",
                ),
            )

    assert observed[0][1].type == "text_delta"
    assert observed[-1][1].type == "reset"
    assert observed[-1][1].data["reason_code"] == "provider_output_truncated"
    assert {turn_id for turn_id, _ in observed} == {observed[0][0]}
    await engine.dispose()


@pytest.mark.asyncio
async def test_provider_io_never_runs_inside_agent_loop_database_transaction():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as db:
        provider = _TransactionProbeProvider(db)
        coordinator = AgentLoopCoordinator(
            ModelAdapter(provider), ActionDispatcher([]),
        )
        context = ContextPackage(
            messages=({"role": "user", "content": "go"},),
            manifest=(), estimated_tokens=1,
            soft_input_target=100, hard_input_limit=200,
        )
        result = await coordinator.run(
            context=context, tool_definitions=[],
            capability_snapshot={"id": "snapshot", "effective_tool_keys": []},
            runtime_context=_runtime_context(), model_parameters={}, db=db,
            identity=LoopIdentity(
                runner_id="runner", work_item_id="work", run_id="run",
                agent_version_id="agent-version", model_version_id="model-version",
                provider="openai",
            ),
        )

    assert result.status == "completed"
    assert provider.transaction_states == [("count", False), ("stream", False)]
    await engine.dispose()
