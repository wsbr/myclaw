from typing import AsyncIterator
import json
import openai as oai
from app.core.model_hub.base import BaseLLM, LLMResponse, LLMChunk, ToolCall, TokenUsage


def normalize_messages_for_openai(messages: list[dict]) -> list[dict]:
    """Convert internal tool-call messages to OpenAI chat completion format."""
    normalized: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            tool_calls = []
            for tc in msg["tool_calls"]:
                if tc.get("function"):
                    tool_calls.append(tc)
                    continue
                args = tc.get("input", tc.get("arguments", {}))
                if isinstance(args, str):
                    args_str = args
                else:
                    args_str = json.dumps(args, ensure_ascii=False)
                tool_calls.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": args_str,
                    },
                })
            normalized.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })
        elif role == "tool" or msg.get("tool_use_id"):
            normalized.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_use_id") or msg.get("tool_call_id"),
                "content": msg.get("content", ""),
            })
        else:
            normalized.append(msg)
    return normalized


class OpenAILLM(BaseLLM):
    def __init__(self, api_key: str, base_url: str | None = None):
        self.client = oai.AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def chat(self, messages: list[dict], tools: list[dict] | None, params: dict) -> LLMResponse:
        kwargs = dict(
            model=params.get("model_id", "gpt-4o"),
            messages=normalize_messages_for_openai(messages),
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
            messages=normalize_messages_for_openai(messages),
            stream=True,
        )
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        async for chunk in await self.client.chat.completions.create(**kwargs):
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield LLMChunk(type="text_delta", content=delta.content)

    async def count_tokens(self, messages: list[dict]) -> int:
        return sum(len(str(m.get("content", ""))) // 4 for m in messages)
