"""Deterministic mock LLM for in-process integration/e2e tests."""

from __future__ import annotations

import json
import re
from typing import AsyncIterator

from app.core.model_hub.base import BaseLLM, LLMChunk, LLMResponse, TokenUsage, ToolCall


DEFAULT_PLAN = {
    "goal": "Analyze quarterly sales performance",
    "steps": [
        {"id": "s1", "description": "Collect sales data from Q3", "tool": None, "depends_on": []},
        {"id": "s2", "description": "Summarize trends and key metrics", "tool": None, "depends_on": ["s1"]},
    ],
}


class FakeLLM(BaseLLM):
    """Returns predictable responses based on prompt content."""

    def __init__(self, plan: dict | None = None):
        self.plan = plan or DEFAULT_PLAN
        self.call_log: list[dict] = []

    async def chat(self, messages: list[dict], tools: list[dict] | None, params: dict) -> LLMResponse:
        self.call_log.append({"mode": "chat", "messages": messages, "params": params, "tools": tools})
        if tool_calls := self._maybe_tool_calls(messages, tools):
            return LLMResponse(
                content="",
                tool_calls=tool_calls,
                usage=TokenUsage(input_tokens=10, output_tokens=20),
                model=params.get("model_id", "fake-model"),
                finish_reason="tool_calls",
            )
        content = self._resolve_content(messages, params)
        return LLMResponse(
            content=content,
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            model=params.get("model_id", "fake-model"),
        )

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        params: dict,
    ) -> AsyncIterator[LLMChunk]:
        self.call_log.append({"mode": "stream", "messages": messages, "params": params, "tools": tools})
        if self._maybe_tool_calls(messages, tools):
            # Empty stream triggers chat() fallback in AgentRuntime.
            yield LLMChunk(type="done")
            return
        content = self._resolve_content(messages, params)
        yield LLMChunk(type="text_delta", content=content)
        yield LLMChunk(type="done")

    async def count_tokens(self, messages: list[dict]) -> int:
        return sum(len(str(m.get("content", ""))) // 4 for m in messages)

    def _resolve_content(self, messages: list[dict], params: dict) -> str:
        if any(m.get("role") == "tool" for m in messages):
            return "已为你创建定时提醒任务。"

        system = params.get("system", "")
        text = " ".join(str(m.get("content", "")) for m in messages)

        if "planning assistant" in system.lower() or "generate a structured execution plan" in text.lower():
            return json.dumps(self.plan, ensure_ascii=False)

        if "revise" in system.lower() or "revised plan json" in text.lower():
            revised = {
                **self.plan,
                "steps": self.plan["steps"] + [
                    {"id": "s3", "description": "Draft executive summary", "tool": None, "depends_on": ["s2"]},
                ],
            }
            return json.dumps(revised, ensure_ascii=False)

        step_match = re.search(r"Current step \((s\d+)\)", text)
        if step_match:
            step_id = step_match.group(1)
            return f"Completed output for {step_id}: deterministic test result."

        return "Mock assistant response."

    def _maybe_tool_calls(self, messages: list[dict], tools: list[dict] | None) -> list[ToolCall]:
        if not tools or any(m.get("role") == "tool" for m in messages):
            return []
        tool_names = {t.get("name") for t in tools}
        if "create_scheduled_task" not in tool_names:
            return []
        user_text = " ".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "user"
        )
        if not any(kw in user_text for kw in ("分钟后", "提醒", "remind", "schedule")):
            return []
        return [
            ToolCall(
                id="call_fake_sched_1",
                name="create_scheduled_task",
                arguments={
                    "title": "复习提醒",
                    "description": "提醒用户复习 BFS",
                    "delay": "5m",
                },
            )
        ]
