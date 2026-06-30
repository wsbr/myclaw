from typing import AsyncIterator
import openai as oai
from app.core.model_hub.base import BaseLLM, LLMResponse, LLMChunk, ToolCall, TokenUsage


class OpenAILLM(BaseLLM):
    def __init__(self, api_key: str, base_url: str | None = None):
        self.client = oai.AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def chat(self, messages: list[dict], tools: list[dict] | None, params: dict) -> LLMResponse:
        kwargs = dict(
            model=params.get("model_id", "gpt-4o"),
            messages=messages,
            max_tokens=params.get("max_tokens", 8192),
            temperature=params.get("temperature", 0.7),
        )
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]

        resp = await self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = []
        if msg.tool_calls:
            import json
            for tc in msg.tool_calls:
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments)))

        return LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            usage=TokenUsage(input_tokens=resp.usage.prompt_tokens, output_tokens=resp.usage.completion_tokens),
            model=resp.model,
            finish_reason=choice.finish_reason,
        )

    async def stream(self, messages: list[dict], tools: list[dict] | None, params: dict) -> AsyncIterator[LLMChunk]:
        kwargs = dict(
            model=params.get("model_id", "gpt-4o"),
            messages=messages,
            stream=True,
        )
        async for chunk in await self.client.chat.completions.create(**kwargs):
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield LLMChunk(type="text_delta", content=delta.content)

    async def count_tokens(self, messages: list[dict]) -> int:
        return sum(len(str(m.get("content", ""))) // 4 for m in messages)
