from typing import AsyncIterator
import anthropic
from app.core.model_hub.base import BaseLLM, LLMResponse, LLMChunk, ToolCall, TokenUsage


class AnthropicLLM(BaseLLM):
    def __init__(self, api_key: str, base_url: str | None = None):
        self.client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)

    async def chat(self, messages: list[dict], tools: list[dict] | None, params: dict) -> LLMResponse:
        system = params.pop("system", None)
        kwargs = dict(
            model=params.get("model_id", "claude-sonnet-4-6"),
            max_tokens=params.get("max_tokens", 8192),
            messages=messages,
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
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
        system = params.pop("system", None)
        kwargs = dict(
            model=params.get("model_id", "claude-sonnet-4-6"),
            max_tokens=params.get("max_tokens", 8192),
            messages=messages,
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        if params.get("temperature") is not None:
            kwargs["temperature"] = params["temperature"]

        async with self.client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield LLMChunk(type="text_delta", content=text)

    async def count_tokens(self, messages: list[dict]) -> int:
        resp = await self.client.messages.count_tokens(
            model="claude-haiku-4-5-20251001",
            messages=messages,
        )
        return resp.input_tokens
