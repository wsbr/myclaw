"""Provider integration conformance through the real OpenAI/Anthropic adapters."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.model_hub.anthropic import AnthropicLLM, normalize_messages_for_anthropic
from app.core.model_hub.openai import OpenAILLM
from app.core.model_hub.registry import ModelHub
from app.models.registry import ModelVersion
from app.services.provider_conformance import ProviderConformanceSuite

pytestmark = pytest.mark.unit


def _version(provider: str) -> ModelVersion:
    return ModelVersion(
        id=f"{provider}-version", model_id=f"{provider}-model", version_no=1,
        provider=provider, model_name="native-test", default_parameters={},
        capabilities={
            "streaming": True, "native_tool_calling": True,
            "tool_result_continuation": True, "stop_reason": True,
            "parallel_tool_calling": False, "usage_in_stream": True,
            "context_window": 8192, "structured_output": "none",
        },
        content_hash="a" * 64,
    )


def _openai_chunk(*, content=None, tool_calls=None, finish_reason=None, usage=None):
    choices = []
    if content is not None or tool_calls is not None or finish_reason is not None:
        choices = [SimpleNamespace(
            delta=SimpleNamespace(content=content, tool_calls=tool_calls),
            finish_reason=finish_reason,
        )]
    return SimpleNamespace(
        id="response-1", model="native-test", choices=choices, usage=usage,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", [
    "openai", "openai_compatible", "azure_openai", "ollama",
])
async def test_openai_family_adapters_pass_conformance_suite(provider_name):
    calls = 0

    class Completions:
        async def create(self, **kwargs):
            nonlocal calls
            calls += 1

            async def chunks():
                if calls == 1:
                    yield _openai_chunk(content="plain")
                    yield _openai_chunk(finish_reason="stop")
                elif calls in {2, 4, 6}:
                    value = {
                        2: "native-v2", 4: "native-v2-repeat", 6: "repaired",
                    }[calls]
                    yield _openai_chunk(tool_calls=[SimpleNamespace(
                        index=0, id=f"call-probe-{calls}",
                        function=SimpleNamespace(
                            name="myclaw_conformance_echo", arguments='{"value":',
                        ),
                    )])
                    yield _openai_chunk(tool_calls=[SimpleNamespace(
                        index=0, id=None,
                        function=SimpleNamespace(name=None, arguments=json.dumps(value) + '}'),
                    )])
                    yield _openai_chunk(finish_reason="tool_calls")
                else:
                    assert kwargs["tools"][0]["function"]["name"] == "myclaw_conformance_echo"
                    assert kwargs["messages"][-1]["role"] == "tool"
                    yield _openai_chunk(content="native-v2")
                    yield _openai_chunk(finish_reason="stop")
                yield _openai_chunk(usage=SimpleNamespace(
                    prompt_tokens=10, completion_tokens=2,
                ))

            return chunks()

    adapter = ModelHub.get({}, provider_name, "unused")
    assert isinstance(adapter, OpenAILLM)
    await adapter.client.close()
    adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    report = await ProviderConformanceSuite(adapter).run(_version(provider_name))
    assert report.passed is True
    assert calls == 6


class _AnthropicStream:
    def __init__(self, final, text):
        self.final = final
        self.events = [SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=value),
        ) for value in text]

    def __aiter__(self):
        self.iterator = iter(self.events)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration as error:
            raise StopAsyncIteration from error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_final_message(self):
        return self.final


@pytest.mark.asyncio
async def test_anthropic_adapter_passes_conformance_suite_and_maps_tool_results():
    calls = []

    class Messages:
        def stream(self, **kwargs):
            calls.append(kwargs)
            index = len(calls)
            if index == 1:
                content = [SimpleNamespace(type="text", text="plain")]
                text = ["plain"]
                reason = "end_turn"
            elif index in {2, 4, 6}:
                value = {2: "native-v2", 4: "native-v2-repeat", 6: "repaired"}[index]
                content = [SimpleNamespace(
                    type="tool_use", id=f"call-probe-{index}",
                    name="myclaw_conformance_echo", input={"value": value},
                )]
                text = []
                reason = "tool_use"
            else:
                content = [SimpleNamespace(type="text", text="native-v2")]
                text = ["native-v2"]
                reason = "end_turn"
            final = SimpleNamespace(
                id=f"anthropic-{index}", model="native-test", content=content,
                stop_reason=reason,
                usage=SimpleNamespace(input_tokens=10, output_tokens=2),
            )
            return _AnthropicStream(final, text)

    adapter = ModelHub.get({}, "anthropic", "unused")
    assert isinstance(adapter, AnthropicLLM)
    await adapter.client.close()
    adapter.client = SimpleNamespace(messages=Messages())
    report = await ProviderConformanceSuite(adapter).run(_version("anthropic"))
    assert report.passed is True
    assert len(calls) == 6
    assert calls[1]["tool_choice"] == {
        "type": "tool", "name": "myclaw_conformance_echo",
    }
    assert calls[2]["messages"][-1]["role"] == "user"
    assert calls[2]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_anthropic_normalization_separates_system_and_preserves_call_correlation():
    system, messages = normalize_messages_for_anthropic([
        {"role": "system", "content": "runtime rules"},
        {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "call-1", "type": "tool_use", "name": "lookup",
                "input": {"key": "alpha"},
            }],
        },
        {"role": "tool", "tool_use_id": "call-1", "content": "42"},
    ])
    assert system == "runtime rules"
    assert messages[0]["content"][0]["id"] == "call-1"
    assert messages[1]["content"][0]["tool_use_id"] == "call-1"


def test_anthropic_normalization_preserves_ordered_turn_items():
    _, messages = normalize_messages_for_anthropic([{
        "role": "assistant",
        "turn_items": [
            {"type": "assistant_message", "content_parts": [{"type": "text", "value": "before"}]},
            {"type": "tool_call", "call_id": "call-1", "name": "lookup", "arguments": {"key": "alpha"}},
            {"type": "assistant_message", "content_parts": [{"type": "text", "value": "after"}]},
        ],
    }])
    assert [item["type"] for item in messages[0]["content"]] == [
        "text", "tool_use", "text",
    ]
