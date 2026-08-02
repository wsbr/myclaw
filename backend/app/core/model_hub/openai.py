from typing import AsyncIterator
import json
import openai as oai
from app.core.model_hub.base import BaseLLM, LLMResponse, LLMChunk, ToolCall, TokenUsage
from app.core.model_hub.tool_wire import to_openai_tools


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
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        tool_strict: bool = False,
    ):
        self.client = oai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.tool_strict = tool_strict

    async def chat(self, messages: list[dict], tools: list[dict] | None, params: dict) -> LLMResponse:
        system = params.get("system")
        msgs = normalize_messages_for_openai(messages)
        if system:
            msgs = [{"role": "system", "content": system}] + msgs

        kwargs = dict(
            model=params.get("model_id", "gpt-4o"),
            messages=msgs,
            max_tokens=params.get("max_tokens", 8192),
            temperature=params.get("temperature", 0.7),
        )
        if tools:
            kwargs["tools"] = to_openai_tools(tools, strict=self.tool_strict)

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
        system = params.get("system")
        msgs = normalize_messages_for_openai(messages)
        if system:
            msgs = [{"role": "system", "content": system}] + msgs

        kwargs = dict(
            model=params.get("model_id", "gpt-4o"),
            messages=msgs,
            stream=True,
            stream_options={"include_usage": True},
        )
        if tools:
            kwargs["tools"] = to_openai_tools(tools, strict=self.tool_strict)

        # OpenAI 流式工具调用是按 index 分片到达的（id/name 只在首个分片出现，
        # arguments 是需要跨多个 chunk 拼接的 JSON 字符串片段），流结束后统一解析。
        tool_call_acc: dict[int, dict] = {}
        usage = None

        async for chunk in await self.client.chat.completions.create(**kwargs):
            if chunk.usage:
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield LLMChunk(type="text_delta", content=delta.content)
            if delta and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    entry = tool_call_acc.setdefault(tc_delta.index, {"id": None, "name": None, "arguments": ""})
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["arguments"] += tc_delta.function.arguments

        for idx in sorted(tool_call_acc):
            entry = tool_call_acc[idx]
            try:
                arguments = json.loads(entry["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            yield LLMChunk(
                type="tool_call_delta",
                tool_call=ToolCall(id=entry["id"] or "", name=entry["name"] or "", arguments=arguments),
            )

        if usage:
            yield LLMChunk(
                type="done",
                usage=TokenUsage(input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens),
            )

    async def count_tokens(self, messages: list[dict]) -> int:
        return sum(len(str(m.get("content", ""))) // 4 for m in messages)
