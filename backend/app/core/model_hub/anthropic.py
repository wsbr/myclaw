from typing import AsyncIterator
import anthropic
from app.core.model_hub.base import BaseLLM, LLMResponse, LLMChunk, ToolCall, TokenUsage
from app.core.model_hub.base import bounded_provider_id, provider_request_identity
from app.core.model_hub.tool_wire import to_anthropic_tools
from app.core.model_hub.parameters import validate_provider_parameters


def _token_usage(usage) -> TokenUsage:
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        cached_input_tokens=getattr(usage, "cache_read_input_tokens", None),
    )


def normalize_messages_for_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert the neutral transcript into Anthropic content blocks."""
    system_parts: list[str] = []
    normalized: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            system_parts.append(str(message.get("content") or ""))
            continue
        if role == "assistant" and message.get("turn_items"):
            content = []
            for item in message["turn_items"]:
                if item.get("type") == "assistant_message":
                    text = "".join(
                        str(part.get("value", ""))
                        for part in item.get("content_parts", [])
                        if part.get("type") == "text"
                    )
                    if text:
                        content.append({"type": "text", "text": text})
                elif item.get("type") == "tool_call":
                    content.append({
                        "type": "tool_use", "id": item["call_id"],
                        "name": item["name"], "input": item.get("arguments") or {},
                    })
            normalized.append({"role": "assistant", "content": content})
            continue
        if role == "assistant" and message.get("tool_calls"):
            content = []
            if message.get("content"):
                content.append({"type": "text", "text": message["content"]})
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                arguments = call.get("input", call.get("arguments", function.get("arguments", {})))
                content.append({
                    "type": "tool_use", "id": call["id"],
                    "name": call.get("name") or function.get("name", ""),
                    "input": arguments,
                })
            normalized.append({"role": "assistant", "content": content})
            continue
        if role == "tool" or message.get("tool_use_id"):
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_use_id") or message.get("tool_call_id"),
                "content": message.get("content", ""),
            }
            if normalized and normalized[-1]["role"] == "user" and isinstance(normalized[-1]["content"], list):
                normalized[-1]["content"].append(block)
            else:
                normalized.append({"role": "user", "content": [block]})
            continue
        normalized.append({"role": role, "content": message.get("content", "")})
    return "\n\n".join(part for part in system_parts if part), normalized


class AnthropicLLM(BaseLLM):
    def __init__(self, api_key: str, base_url: str | None = None):
        self.client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=0)
        self.tool_strict = True

    async def chat(self, messages: list[dict], tools: list[dict] | None, params: dict) -> LLMResponse:
        validate_provider_parameters(params, provider="anthropic")
        message_system, normalized_messages = normalize_messages_for_anthropic(messages)
        system = message_system
        kwargs = dict(
            model=params.get("model_id", "claude-sonnet-4-6"),
            max_tokens=params.get("max_tokens", 8192),
            messages=normalized_messages,
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = to_anthropic_tools(tools, strict=self.tool_strict)
            if params.get("tool_choice"):
                choice = params["tool_choice"]
                if choice == "required":
                    kwargs["tool_choice"] = {"type": "any"}
                elif isinstance(choice, dict):
                    function = choice.get("function") or {}
                    name = choice.get("name") or function.get("name")
                    kwargs["tool_choice"] = (
                        {"type": "tool", "name": name} if name else choice
                    )
        if params.get("temperature") is not None:
            kwargs["temperature"] = params["temperature"]

        resp = await self.client.messages.create(**kwargs)
        request_id, request_id_source = provider_request_identity(resp)
        response_id = bounded_provider_id(getattr(resp, "id", None))

        tool_calls = []
        text_content = ""
        for block in resp.content:
            if block.type == "text":
                text_content = block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input))

        return LLMResponse(
            content=text_content,
            tool_calls=tool_calls,
            usage=_token_usage(resp.usage),
            model=resp.model,
            finish_reason=resp.stop_reason or "stop",
            provider_request_id=request_id or response_id,
            provider_request_id_source=request_id_source or ("response_id_fallback" if response_id else None),
            provider_response_id=response_id,
        )

    async def stream(self, messages: list[dict], tools: list[dict] | None, params: dict) -> AsyncIterator[LLMChunk]:
        validate_provider_parameters(params, provider="anthropic")
        message_system, normalized_messages = normalize_messages_for_anthropic(messages)
        system = message_system
        kwargs = dict(
            model=params.get("model_id", "claude-sonnet-4-6"),
            max_tokens=params.get("max_tokens", 8192),
            messages=normalized_messages,
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = to_anthropic_tools(tools, strict=self.tool_strict)
            if params.get("tool_choice"):
                choice = params["tool_choice"]
                if choice == "required":
                    kwargs["tool_choice"] = {"type": "any"}
                elif isinstance(choice, dict):
                    function = choice.get("function") or {}
                    name = choice.get("name") or function.get("name")
                    kwargs["tool_choice"] = (
                        {"type": "tool", "name": name} if name else choice
                    )
        if params.get("temperature") is not None:
            kwargs["temperature"] = params["temperature"]

        # SDK assembles the final tool_use blocks. Raw Tool events fence retries
        # while their JSON is incomplete; lifecycle, thinking and usage events
        # are metadata and remain safe to retry after a transport failure.
        async with self.client.messages.stream(**kwargs) as stream:
            request_id, request_id_source = provider_request_identity(getattr(stream, "response", None))
            response_id = None
            if request_id:
                yield LLMChunk(type="metadata", provider_request_id=request_id,
                               provider_request_id_source=request_id_source)
            async for event in stream:
                event_type = getattr(event, "type", None)
                if event_type == "message_start":
                    response_id = bounded_provider_id(getattr(getattr(event, "message", None), "id", None))
                    if response_id:
                        yield LLMChunk(type="metadata", provider_request_id=request_id,
                                       provider_request_id_source=request_id_source,
                                       provider_response_id=response_id)
                delta_type = getattr(getattr(event, "delta", None), "type", None)
                block_type = getattr(getattr(event, "content_block", None), "type", None)
                if event_type == "content_block_start" and block_type == "tool_use":
                    yield LLMChunk(type="transport_payload")
                elif event_type == "content_block_delta" and delta_type == "input_json_delta":
                    yield LLMChunk(type="transport_payload")
                if event_type == "content_block_delta" and delta_type == "text_delta":
                    yield LLMChunk(
                        type="text_delta", content=event.delta.text,
                        item_index=getattr(event, "index", 0),
                    )
            final_message = await stream.get_final_message()

        for index, block in enumerate(final_message.content):
            if block.type == "tool_use":
                yield LLMChunk(
                    type="tool_call_delta",
                    tool_call=ToolCall(id=block.id, name=block.name, arguments=block.input),
                    item_index=index,
                )

        yield LLMChunk(
            type="done",
            usage=_token_usage(final_message.usage),
            stop_reason=final_message.stop_reason,
            model=final_message.model,
            provider_request_id=request_id,
            provider_request_id_source=request_id_source,
            provider_response_id=bounded_provider_id(getattr(final_message, "id", None)) or response_id,
        )

    async def count_tokens(
        self, messages: list[dict], tools: list[dict] | None = None,
        params: dict | None = None,
    ) -> int:
        params = dict(params or {})
        validate_provider_parameters(params, provider="anthropic")
        message_system, normalized_messages = normalize_messages_for_anthropic(messages)
        system = message_system
        kwargs = {
            "model": params.get("model_id", "claude-sonnet-4-6"),
            "messages": normalized_messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = to_anthropic_tools(tools, strict=self.tool_strict)
        count_tokens = getattr(self.client.messages, "count_tokens", None)
        if count_tokens is None:
            # The pinned 0.40 SDK exposes this endpoint under beta.messages.
            count_tokens = self.client.beta.messages.count_tokens
        resp = await count_tokens(**kwargs)
        return resp.input_tokens
