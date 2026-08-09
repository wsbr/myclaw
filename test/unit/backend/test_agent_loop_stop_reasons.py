"""Provider stop reasons are enforced before any partial output is published."""

import pytest

from app.core.model_hub.base import BaseLLM, LLMChunk, LLMResponse, TokenUsage, ToolCall
from app.core.tool_registry.base import RuntimeContext
from app.runtime.action_dispatcher import ActionDispatcher
from app.runtime.agent_loop_coordinator import AgentLoopCoordinator, AgentLoopError
from app.runtime.context_assembler import ContextPackage
from app.runtime.model_adapter import ModelAdapter

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


def _context() -> ContextPackage:
    return ContextPackage(
        messages=({"role": "user", "content": "go"},),
        manifest=(), estimated_tokens=1,
    )


def _runtime_context() -> RuntimeContext:
    return RuntimeContext(
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
