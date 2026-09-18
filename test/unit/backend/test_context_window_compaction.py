"""Complete request counting drives soft compaction and hard Context rejection."""

import hashlib
import json

import pytest

from app.core.model_hub.base import BaseLLM, LLMChunk, LLMResponse, TokenUsage
from app.core.tool_registry.base import RuntimeContext
from app.runtime.action_dispatcher import ActionDispatcher
from app.runtime.agent_loop_coordinator import (
    AgentLoopCoordinator, AgentLoopError, LoopIdentity,
)
from app.runtime.context_assembler import ContextAssembler, ContextPackage, request_manifest
from app.runtime.model_adapter import ModelAdapter
from app.runtime.token_counting import conservative_request_tokens, count_provider_request

pytestmark = pytest.mark.unit


class _CountingProvider(BaseLLM):
    def __init__(self, count: int = 1, *, fail_count: bool = False) -> None:
        self.count = count
        self.fail_count = fail_count
        self.stream_calls = 0
        self.counted_tools = None

    async def chat(self, messages, tools, params):
        return LLMResponse(
            content="ok", tool_calls=[], usage=TokenUsage(), model="test",
        )

    async def stream(self, messages, tools, params):
        self.stream_calls += 1
        yield LLMChunk(type="text_delta", content="ok")
        yield LLMChunk(
            type="done", stop_reason="stop", model="test",
            usage=TokenUsage(input_tokens=self.count, output_tokens=1),
        )

    async def count_tokens(self, messages, tools=None, params=None):
        self.counted_tools = tools
        if self.fail_count:
            raise RuntimeError("count service unavailable")
        return self.count


def test_soft_watermark_trims_only_old_history_without_fake_hash_summary():
    history = [
        {"role": "assistant", "content": f"old-{index}-" + "x" * 180}
        for index in range(8)
    ]
    pending = [{"role": "tool", "tool_call_id": "call-1", "content": "result"}]
    tool = {
        "name": "large_tool", "description": "d" * 240,
        "input_schema": {
            "type": "object", "properties": {
                "query": {"type": "string", "description": "q" * 240},
            },
        },
    }

    package = ContextAssembler().assemble(
        platform_rules="platform", agent_instructions="agent",
        current_input="current request", history=history,
        working_plan={
            "revision": 3,
            "items": [{"text": "finish", "status": "in_progress"}],
        },
        pending_results=pending, tool_definitions=[tool],
        soft_input_target=420, hard_input_limit=600,
    )

    contents = [str(item.get("content") or "") for item in package.messages]
    assert package.compaction["reason"] == "soft_input_target_exceeded"
    assert package.compaction["omitted_count"] > 0
    assert package.history_count < len(history)
    assert not any("Compacted context fact index" in item for item in contents)
    assert any(item.startswith("WorkingPlan:") for item in contents)
    assert {"role": "user", "content": "current request"} in package.messages
    assert pending[0] in package.messages
    assert len(package.manifest) == len(package.messages)
    for index, (entry, message) in enumerate(zip(package.manifest, package.messages)):
        assert entry["message_index"] == index
        assert entry["role"] == message["role"]
        assert entry["checksum"] == hashlib.sha256(json.dumps(
            message, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
    assert next(item for item in package.messages if item["content"].startswith("WorkingPlan:"))["role"] == "assistant"
    assert len(package.compaction["omitted_sections"]) == package.compaction["omitted_count"]


def test_provider_compaction_keeps_provenance_and_input_objects_unchanged():
    history = [{"role": "assistant", "content": "original history"}]
    pending = [{"role": "tool", "tool_call_id": "call-1", "content": "original result"}]
    original_pending = request_manifest(pending, [{"source": "pending_result"}])
    assembler = ContextAssembler()
    package = assembler.assemble(
        platform_rules="platform", agent_instructions="agent", current_input="current",
        history=history, history_manifest=[{"source_message_id": "message-1"}],
        pending_results=[{**pending[0], "content": "excerpt"}],
        pending_manifest=original_pending, web_result_budget_ratio=0.35,
    )
    compacted = assembler.drop_oldest_history(package, reason="provider_count_above_soft_target")
    assert compacted.history_count == 0
    assert compacted.web_result_budget_ratio == 0.35
    assert len(compacted.messages) == len(compacted.manifest)
    assert compacted.compaction["omitted_sections"][0]["source_message_id"] == "message-1"
    result_manifest = compacted.manifest[-1]
    assert result_manifest["source_checksum"] == original_pending[0]["checksum"]
    assert result_manifest["checksum"] != result_manifest["source_checksum"]
    assert result_manifest["treatment"] == "projection"
    assert history == [{"role": "assistant", "content": "original history"}]
    assert pending[0]["content"] == "original result"
    assert package.history_count == 1
    assert package.manifest[package.history_start]["source_message_id"] == "message-1"


def test_tool_schema_is_part_of_complete_conservative_request_count():
    messages = [{"role": "user", "content": "hello"}]
    small = conservative_request_tokens(messages, [])
    with_schema = conservative_request_tokens(messages, [{
        "name": "search", "description": "x" * 400,
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "y" * 400},
        }},
    }])
    assert with_schema > small + 200


@pytest.mark.asyncio
async def test_hard_window_rejects_before_generation_and_count_failure_degrades():
    provider = _CountingProvider(count=101)
    context = ContextPackage(
        messages=({"role": "user", "content": "immutable current input"},),
        manifest=(), estimated_tokens=1,
        soft_input_target=80, hard_input_limit=100,
    )
    coordinator = AgentLoopCoordinator(
        ModelAdapter(provider), ActionDispatcher([]),
    )
    identity = LoopIdentity(
        runner_id="runner", work_item_id="work", run_id="run",
        agent_version_id="agent-version", model_version_id="model-version",
        provider="openai",
    )
    with pytest.raises(AgentLoopError) as raised:
        await coordinator.run(
            context=context, tool_definitions=[],
            capability_snapshot={"id": "snapshot", "effective_tool_keys": []},
            runtime_context=RuntimeContext(
                tenant_id="test-tenant",
                session_id="session", runner_id="runner",
                user_id="user", agent_id="agent",
            ),
            model_parameters={"model_id": "test"}, identity=identity,
        )
    assert raised.value.code == "hard_compaction_no_source"
    assert provider.stream_calls == 0

    degraded_provider = _CountingProvider(fail_count=True)
    counted = await count_provider_request(
        degraded_provider, provider_type="openai",
        messages=context.messages, tools=[{"name": "tool"}], parameters={},
    )
    assert counted.degraded is True
    assert counted.method == "conservative_utf8_structural_fallback"
    assert counted.tokens > 0
