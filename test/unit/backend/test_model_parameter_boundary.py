"""Model configuration cannot override Runtime messages, tools or request fields."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest
from pydantic import ValidationError

from app.core.model_hub.anthropic import AnthropicLLM
from app.core.model_hub.openai import OpenAILLM
from app.core.model_hub.parameters import runtime_model_defaults
from app.runtime.model_adapter import ModelAdapter
from app.runtime.protocol import ProviderError, ProviderRequest
from app.runtime.token_counting import count_provider_request
from app.schemas.registry_v1 import ModelCreate, ModelPatch, ModelVersionCreate
from app.services.maintenance_command_service import _provider_parameters
from app.services.provider_conformance import _base_probe_parameters

pytestmark = pytest.mark.unit

MESSAGES = [
    {"role": "system", "content": "Runtime governed instructions"},
    {"role": "user", "content": "Perform the authorized task"},
]
TOOLS = [{"name": "bound_tool", "description": "An explicitly bound tool", "input_schema": {"type": "object", "properties": {}}}]
UNTRUSTED_CONTENT = "configuration-content-must-not-appear-in-diagnostics"
RESERVED = ("system", "messages", "input", "instructions", "tools", "functions", "model", "model_id", "tool_choice", "function_call", "response_format", "stream", "stream_options", "parallel_tool_calls", "extra_headers", "extra_query", "timeout", "base_url", "endpoint")


def registration(schema, parameters):
    if schema is ModelPatch:
        return schema(default_parameters=parameters)
    values = dict(provider="openai_compatible", model_name="unit-model", endpoint="https://provider.invalid/v1", api_key="unit-placeholder", default_parameters=parameters, capabilities={"context_window": 32768, "max_output_tokens": 8192})
    if schema is ModelCreate:
        values["key"] = "unit-model"
    return schema(**values)


@pytest.mark.parametrize("schema", [ModelCreate, ModelVersionCreate, ModelPatch])
@pytest.mark.parametrize("key", RESERVED)
def test_registration_rejects_runtime_fields_in_model_defaults(schema, key):
    with pytest.raises(ValidationError, match="reserved for Runtime"):
        registration(schema, {key: UNTRUSTED_CONTENT})


@pytest.mark.parametrize("schema", [ModelCreate, ModelVersionCreate, ModelPatch])
@pytest.mark.parametrize("extra", [
    {"tools": [{"type": "web_search"}]},
    {"messages": [{"role": "system", "content": UNTRUSTED_CONTENT}]},
    {"model": "other-model"}, {"system": UNTRUSTED_CONTENT},
    {"enable_search": True}, {"plugins": [{"id": "web"}]},
    {"max_tokens": 1000000}, {"temperature": 1},
    {"enable_thinking": "false"}, {"enable_thinking": {"messages": []}},
    None, [],
])
def test_registration_rejects_extra_body_overrides_unknown_extensions_and_wrong_types(schema, extra):
    with pytest.raises(ValidationError):
        registration(schema, {"extra_body": extra})


@pytest.mark.parametrize("thinking", [True, False])
def test_valid_defaults_preserve_existing_thinking_configuration_and_embedding_parameters(thinking):
    parameters = {"max_tokens": 4096, "temperature": 0, "extra_body": {"enable_thinking": thinking}}
    for schema in (ModelCreate, ModelVersionCreate, ModelPatch):
        assert registration(schema, parameters).default_parameters == parameters
    assert ModelVersionCreate(provider="openai_compatible", model_name="embedding", endpoint="https://provider.invalid/v1", api_key="unit-placeholder", default_parameters={"dimensions": 1024, "encoding_format": "float"}, capabilities={"model_kind": "embedding", "embedding_dimensions": 1024}).default_parameters["dimensions"] == 1024
    copied = runtime_model_defaults(parameters)
    copied["extra_body"]["enable_thinking"] = not thinking
    assert parameters["extra_body"]["enable_thinking"] is thinking


def make_provider(kind, handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    if kind == "openai":
        provider = OpenAILLM.__new__(OpenAILLM)
        provider.client = openai.AsyncOpenAI(api_key="unit-placeholder", base_url="https://provider.invalid/v1", http_client=client)
        provider.tool_strict = False
    else:
        provider = AnthropicLLM.__new__(AnthropicLLM)
        provider.client = anthropic.AsyncAnthropic(api_key="unit-placeholder", base_url="https://provider.invalid", http_client=client)
        provider.tool_strict = True
    return provider


async def invoke(provider, operation, parameters):
    if operation == "stream":
        return [chunk async for chunk in provider.stream(MESSAGES, TOOLS, parameters)]
    if operation == "count":
        return await provider.count_tokens(MESSAGES, TOOLS, parameters)
    return await provider.chat(MESSAGES, TOOLS, parameters)


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize("operation", ["chat", "stream", "count"])
@pytest.mark.parametrize("parameters", [
    {"system": UNTRUSTED_CONTENT},
    {"messages": []}, {"tools": []},
    {"extra_body": {"tools": [{"type": "function", "function": {"name": "unbound_tool"}}]}},
    {"extra_body": {"messages": [{"role": "system", "content": UNTRUSTED_CONTENT}]}},
    {"extra_body": {"model": "other-model"}},
    {"extra_body": {"system": UNTRUSTED_CONTENT}},
    {"extra_body": {"enable_search": True}},
    {"extra_body": {"enable_thinking": 1}},
])
async def test_every_provider_exit_rejects_unsafe_frozen_parameters_before_transport(kind, operation, parameters):
    sent = []
    def handler(request):
        sent.append(request)
        return httpx.Response(500)
    provider = make_provider(kind, handler)
    try:
        with pytest.raises(ProviderError) as raised:
            await invoke(provider, operation, {"model_id": "unit-model", **parameters})
        assert raised.value.code == "model_parameters_invalid"
        assert raised.value.retryable is False
        assert raised.value.received_payload is False
        assert UNTRUSTED_CONTENT not in str(raised.value)
        assert sent == []
    finally:
        await provider.client.close()


def mock_response(kind, request):
    body = json.loads(request.content)
    if request.url.path.endswith("/count_tokens"):
        return httpx.Response(200, json={"input_tokens": 42})
    if kind == "openai":
        if body.get("stream"):
            chunks = [
                {"id": "unit", "object": "chat.completion.chunk", "created": 0, "model": "unit-model", "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]},
                {"id": "unit", "object": "chat.completion.chunk", "created": 0, "model": "unit-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="".join("data: " + json.dumps(item) + "\n\n" for item in chunks) + "data: [DONE]\n\n")
        return httpx.Response(200, json={"id": "unit", "object": "chat.completion", "created": 0, "model": "unit-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}})
    message = {"id": "unit", "type": "message", "role": "assistant", "model": "unit-model", "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}
    if not body.get("stream"):
        return httpx.Response(200, json=message)
    events = [
        {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="".join(f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in events))


@pytest.mark.parametrize("kind,operation", [("openai", "chat"), ("openai", "stream"), ("anthropic", "chat"), ("anthropic", "stream"), ("anthropic", "count")])
async def test_real_sdk_wire_preserves_runtime_messages_bound_tools_and_allowed_extensions(kind, operation):
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return mock_response(kind, request)
    defaults = {"temperature": 0, "max_tokens": 256}
    if kind == "openai":
        defaults["extra_body"] = {"enable_thinking": False}
    validated = registration(ModelVersionCreate, defaults)
    parameters = {**runtime_model_defaults(validated.default_parameters), "model_id": "unit-model", "tool_choice": "required"}
    provider = make_provider(kind, handler)
    try:
        await invoke(provider, operation, parameters)
        assert len(sent) == 1
        body = sent[0]
        assert body["model"] == "unit-model"
        if kind == "openai":
            assert body["messages"] == MESSAGES
            assert [tool["function"]["name"] for tool in body["tools"]] == ["bound_tool"]
            assert body["enable_thinking"] is False
            assert body["tool_choice"] == "required"
        else:
            assert body["system"] == MESSAGES[0]["content"]
            assert body["messages"] == MESSAGES[1:]
            assert [tool["name"] for tool in body["tools"]] == ["bound_tool"]
            assert "enable_thinking" not in body
    finally:
        await provider.client.close()


async def test_configuration_error_is_not_retried_or_hidden_by_token_estimation_fallback():
    sent = []
    provider = make_provider("openai", lambda request: sent.append(request) or httpx.Response(500))
    params = {"model_id": "unit-model", "extra_body": {"messages": []}}
    events = []
    try:
        with pytest.raises(ProviderError, match="extra_body.messages") as raised:
            await ModelAdapter(provider).invoke(ProviderRequest(messages=tuple(MESSAGES), tools=tuple(TOOLS), parameters=params), attempt_observer=events.append)
        assert raised.value.code == "model_parameters_invalid"
        assert [event.phase for event in events] == ["started", "failed"]
        with pytest.raises(ProviderError) as count_error:
            await count_provider_request(provider, provider_type="openai", messages=MESSAGES, tools=TOOLS, parameters=params)
        assert count_error.value.code == "model_parameters_invalid"
        assert sent == []
    finally:
        await provider.client.close()


async def test_openai_count_validates_then_tokenizes_only_runtime_messages_and_bound_tools(monkeypatch):
    encoded = []
    class Encoding:
        def encode(self, text):
            encoded.append(text)
            return list(text)
    # Keep this unit test offline; real tokenizer vocabularies may download on
    # first use. The request structure being counted remains the real adapter's.
    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(encoding_for_model=lambda model: Encoding()))
    provider = make_provider("openai", lambda request: pytest.fail("OpenAI token counting must be local"))
    try:
        result = await provider.count_tokens(MESSAGES, TOOLS, {"model_id": "unit-model", "extra_body": {"enable_thinking": False}})
        assert result > 0
        assert [json.loads(encoded[0]), json.loads(encoded[1])] == MESSAGES
        assert [tool["function"]["name"] for tool in json.loads(encoded[2])] == ["bound_tool"]
    finally:
        await provider.client.close()


@pytest.mark.parametrize("parameters", [{"system": UNTRUSTED_CONTENT}, {"model_id": "other-model"}, {"tool_choice": "required"}, {"response_format": {"type": "json_object"}}])
def test_frozen_defaults_are_rejected_before_runtime_maintenance_and_conformance_add_fields(parameters):
    version = SimpleNamespace(default_parameters=parameters, model_name="unit-model")
    for operation in (lambda: runtime_model_defaults(parameters), lambda: _provider_parameters(version, max_tokens=256, forced_tool="trusted_extraction"), lambda: _base_probe_parameters(version)):
        with pytest.raises(ProviderError) as raised:
            operation()
        assert raised.value.code == "model_parameters_invalid"


@pytest.mark.parametrize("operation", ["chat", "stream", "count"])
async def test_anthropic_rejects_unsupported_extensions_instead_of_silently_ignoring_them(operation):
    sent = []
    provider = make_provider("anthropic", lambda request: sent.append(request) or httpx.Response(500))
    try:
        for parameters in ({"extra_body": {"enable_thinking": True}}, {"response_format": {"type": "json_object"}}):
            with pytest.raises(ProviderError) as raised:
                await invoke(provider, operation, parameters)
            assert raised.value.code == "model_parameters_invalid"
        assert sent == []
    finally:
        await provider.client.close()
