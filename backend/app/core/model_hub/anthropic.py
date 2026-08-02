from typing import AsyncIterator
import anthropic
from app.core.model_hub.base import BaseLLM, LLMResponse, LLMChunk, ToolCall, TokenUsage
from app.core.model_hub.tool_wire import to_anthropic_tools


class AnthropicLLM(BaseLLM):
    def __init__(self, api_key: str, base_url: str | None = None):
        self.client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
        self.tool_strict = True

    async def chat(self, messages: list[dict], tools: list[dict] | None, params: dict) -> LLMResponse:
        system = params.get("system")
        kwargs = dict(
            model=params.get("model_id", "claude-sonnet-4-6"),
            max_tokens=params.get("max_tokens", 8192),
            messages=messages,
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = to_anthropic_tools(tools, strict=self.tool_strict)
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
        system = params.get("system")
        kwargs = dict(
            model=params.get("model_id", "claude-sonnet-4-6"),
            max_tokens=params.get("max_tokens", 8192),
            messages=messages,
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = to_anthropic_tools(tools, strict=self.tool_strict)
        if params.get("temperature") is not None:
            kwargs["temperature"] = params["temperature"]

        # 用 SDK 自带的 get_final_message() 拿完整组装好的 tool_use 块和真实 usage，
        # 而不是手写增量 JSON 片段累积器——messages.stream() 本来就已经做好了这层工作。
        async with self.client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield LLMChunk(type="text_delta", content=text)
            final_message = await stream.get_final_message()

        for block in final_message.content:
            if block.type == "tool_use":
                yield LLMChunk(
                    type="tool_call_delta",
                    tool_call=ToolCall(id=block.id, name=block.name, arguments=block.input),
                )

        yield LLMChunk(
            type="done",
            usage=TokenUsage(
                input_tokens=final_message.usage.input_tokens,
                output_tokens=final_message.usage.output_tokens,
            ),
        )

    async def count_tokens(self, messages: list[dict]) -> int:
        resp = await self.client.messages.count_tokens(
            model="claude-haiku-4-5-20251001",
            messages=messages,
        )
        return resp.input_tokens
