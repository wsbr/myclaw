"""
OpenAI-compatible mock LLM server for full-stack deployment tests.

Supports both streaming and non-streaming chat completions.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="MyClaw Mock LLM")


class ChatMessage(BaseModel):
    role: str
    content: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str = "mock-model"
    messages: list[ChatMessage] = []
    stream: bool = False
    tools: list[dict] | None = None


SCHEDULED_TOOL_ARGS = {
    "title": "复习提醒",
    "description": "提醒用户复习 BFS",
    "delay": "5m",
}


def _tool_names(tools: list[dict] | None) -> set[str]:
    names: set[str] = set()
    if not tools:
        return names
    for t in tools:
        fn = t.get("function") or t
        name = fn.get("name") or t.get("name")
        if name:
            names.add(name)
    return names


def _should_schedule_tool(messages: list[ChatMessage], tools: list[dict] | None) -> bool:
    if "create_scheduled_task" not in _tool_names(tools):
        return False
    user_msgs = [m.content for m in messages if m.role == "user" and m.content]
    if not user_msgs:
        return False
    text = user_msgs[-1]
    return any(kw in text for kw in ("分钟后", "提醒", "remind", "schedule"))


def _has_tool_result(messages: list[ChatMessage]) -> bool:
    return any(m.role == "tool" for m in messages)


PLAN_JSON = {
    "goal": "Analyze quarterly sales performance",
    "steps": [
        {"id": "s1", "description": "Collect sales data from Q3", "tool": None, "depends_on": []},
        {"id": "s2", "description": "Summarize trends and key metrics", "tool": None, "depends_on": ["s1"]},
    ],
}


def _detect_step_id(messages: list[ChatMessage]) -> str | None:
    text = " ".join(m.content or "" for m in messages)
    match = re.search(r"Current step \((s\d+)\)", text)
    return match.group(1) if match else None


def _is_plan_generation(messages: list[ChatMessage]) -> bool:
    text = " ".join(m.content or "" for m in messages).lower()
    return "generate a structured execution plan" in text or "generate a structured" in text


def _is_plan_revise(messages: list[ChatMessage]) -> bool:
    text = " ".join(m.content or "" for m in messages).lower()
    return "revised plan json" in text or "user instruction" in text


def _build_content(messages: list[ChatMessage]) -> str:
    if _has_tool_result(messages):
        return "已为你创建定时提醒任务。"
    if _is_plan_generation(messages):
        return json.dumps(PLAN_JSON, ensure_ascii=False)
    if _is_plan_revise(messages):
        revised = {**PLAN_JSON, "steps": PLAN_JSON["steps"] + [
            {"id": "s3", "description": "Draft executive summary", "tool": None, "depends_on": ["s2"]},
        ]}
        return json.dumps(revised, ensure_ascii=False)
    step_id = _detect_step_id(messages)
    if step_id:
        return f"Completed output for {step_id}: mock analysis result."
    # General chat response
    user_msgs = [m.content for m in messages if m.role == "user" and m.content]
    if user_msgs:
        return f"Mock assistant reply to: {user_msgs[-1][:80]}"
    return "Mock LLM response for general query."


def _completion_payload(content: str, model: str, *, tool_calls: list[dict] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = "stop"
    if tool_calls:
        message["content"] = None
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def _scheduled_tool_calls() -> list[dict]:
    return [{
        "id": "call_mock_sched_1",
        "type": "function",
        "function": {
            "name": "create_scheduled_task",
            "arguments": json.dumps(SCHEDULED_TOOL_ARGS, ensure_ascii=False),
        },
    }]


def _stream_chunks(content: str, model: str, *, empty: bool = False):
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    yield (
        "data: " + json.dumps({
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }) + "\n\n"
    )
    if not empty:
        yield (
            "data: " + json.dumps({
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
            }) + "\n\n"
        )
    yield (
        "data: " + json.dumps({
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }) + "\n\n"
    )
    yield "data: [DONE]\n\n"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest):
    if _should_schedule_tool(body.messages, body.tools) and not _has_tool_result(body.messages):
        if body.stream:
            return StreamingResponse(
                _stream_chunks("", body.model, empty=True),
                media_type="text/event-stream",
            )
        return _completion_payload("", body.model, tool_calls=_scheduled_tool_calls())

    content = _build_content(body.messages)
    if body.stream:
        return StreamingResponse(
            _stream_chunks(content, body.model),
            media_type="text/event-stream",
        )
    return _completion_payload(content, body.model)
