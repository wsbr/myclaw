from typing import AsyncIterator
import anthropic
from app.core.model_hub.base import BaseLLM, LLMResponse, LLMChunk, ToolCall, TokenUsage
from app.core.model_hub.tool_wire import to_anthropic_tools


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
        self.client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
        self.tool_strict = True

    async def chat(self, messages: list[dict], tools: list[dict] | None, params: dict) -> LLMResponse:
        message_system, normalized_messages = normalize_messages_for_anthropic(messages)
        system = "\n\n".join(part for part in (params.get("system"), message_system) if part)
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
            usage=TokenUsage(input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens),
            model=resp.model,
            finish_reason=resp.stop_reason or "stop",
        )

    async def stream(self, messages: list[dict], tools: list[dict] | None, params: dict) -> AsyncIterator[LLMChunk]:
        message_system, normalized_messages = normalize_messages_for_anthropic(messages)
        system = "\n\n".join(part for part in (params.get("system"), message_system) if part)
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

        # SDK assembles the final tool_use blocks, while raw events let the
        # ModelAdapter fence retries as soon as any Provider payload arrives.
        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                yield LLMChunk(type="transport_payload")
                if (
                    getattr(event, "type", None) == "content_block_delta"
                    and getattr(getattr(event, "delta", None), "type", None) == "text_delta"
                ):
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
            usage=TokenUsage(
                input_tokens=final_message.usage.input_tokens,
                output_tokens=final_message.usage.output_tokens,
            ),
            stop_reason=final_message.stop_reason,
            model=final_message.model,
            provider_request_id=getattr(final_message, "id", None),
        )

    async def count_tokens(self, messages: list[dict]) -> int:
        resp = await self.client.messages.count_tokens(
            model="claude-haiku-4-5-20251001",
            messages=messages,
        )
        return resp.input_tokens
