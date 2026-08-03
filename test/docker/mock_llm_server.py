"""
OpenAI-compatible mock LLM server for full-stack deployment tests.

Supports both streaming and non-streaming chat completions.
"""
from __future__ import annotations

import asyncio
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


def _should_execute_tool(messages: list[ChatMessage], tools: list[dict] | None) -> bool:
    if "execute" not in _tool_names(tools):
        return False
    user_msgs = [m.content for m in messages if m.role == "user" and m.content]
    return bool(user_msgs and "sandbox command" in user_msgs[-1].lower())


def _execute_arguments(messages: list[ChatMessage]) -> dict:
    user_msgs = [m.content or "" for m in messages if m.role == "user"]
    if user_msgs and "sandbox file" in user_msgs[-1].lower():
        return {
            "argv": [
                "python", "-c",
                "from pathlib import Path; p=Path('acceptance.txt'); p.write_text('sandbox-e2e'); print(p.read_text())",
            ],
            "timeout_seconds": 5,
        }
    return {"argv": ["/bin/echo", "sandbox-e2e"], "timeout_seconds": 5}


def _requested_control_tool(messages: list[ChatMessage], tools: list[dict] | None) -> tuple[str, dict] | None:
    if _has_tool_result(messages):
        return None
    names = _tool_names(tools)
    user_messages = [m.content or "" for m in messages if m.role == "user"]
    text = user_messages[-1].lower() if user_messages else ""
    if ("ask clarification" in text or "request_clarification" in text) and "request_clarification" in names:
        return "request_clarification", {"question": "Which reporting period should the Group use?"}
    coordination = re.search(
        r'"plan_node_key":"([^"]+)","assigned_agent_id":"([0-9a-f-]{36})"',
        text,
    )
    if coordination and "invoke_agent" in names:
        node_key, agent_id = coordination.groups()
        return "invoke_agent", {
            "agent_id": agent_id,
            "plan_node_key": node_key,
            "task": f"Execute frozen Plan node {node_key}",
        }
    match = re.search(r"invoke(?:_| )agent(?: once)?(?: for agent)? ([0-9a-f-]{36})", text)
    if match and "invoke_agent" in names:
        return "invoke_agent", {"agent_id": match.group(1), "task": "Provide a secondary verification"}
    if ("search shared history" in text or "search_group_history" in text) and "search_group_history" in names:
        return "search_group_history", {"query": "group", "limit": 20}
    return None


def _requested_artifact_tool(messages: list[ChatMessage], tools: list[dict] | None) -> tuple[str, dict] | None:
    names = _tool_names(tools)
    user_messages = [m.content or "" for m in messages if m.role == "user"]
    user_text = user_messages[-1].lower() if user_messages else ""
    if "publish acceptance artifact" not in user_text:
        return None
    tool_results = " ".join(m.content or "" for m in messages if m.role == "tool")
    if not tool_results and "write_file" in names:
        return "write_file", {
            "path": "验收报告 2026.txt",
            "content": "artifact-e2e",
            "mode": "create",
        }
    if "验收报告 2026.txt" in tool_results and '"artifact"' not in tool_results and "publish_artifact" in names:
        return "publish_artifact", {
            "path": "验收报告 2026.txt",
            "name": "验收报告 2026.txt",
            "mime_type": "text/plain",
            "publish_to_group": False,
        }
    return None


def _has_tool_result(messages: list[ChatMessage]) -> bool:
    return any(m.role == "tool" for m in messages)


def _catalog_agent_id(messages: list[ChatMessage], flag: str) -> str:
    text = " ".join(message.content or "" for message in messages)
    match = re.search(rf'"agent_id":"([^"]+)"[^}}]*"{flag}":true', text)
    if match:
        return match.group(1)
    match = re.search(r'"agent_id":"([^"]+)"', text)
    return match.group(1) if match else "00000000-0000-0000-0000-000000000001"


def _plan_json(messages: list[ChatMessage]) -> dict:
    return {
        "goal": "Analyze quarterly sales performance",
        "deliverable": "A concise analysis summary",
        "nodes": [{
            "node_key": "s1", "title": "Analyze sales", "goal": "Analyze the supplied sales request",
            "acceptance_criteria": "Return a concise result",
            "assigned_agent_id": _catalog_agent_id(messages, "is_owner"),
        }],
        "dependencies": [],
    }


def _group_json(messages: list[ChatMessage]) -> dict:
    return {
        "goal": "Collaboratively analyze the request",
        "bootstrap_context": "Use only the confirmed request context.",
        "participants": [{
            "participant_key": "owner", "agent_id": _catalog_agent_id(messages, "is_entry"),
            "is_entry": True, "initial_responsibility": "Own analysis and delivery",
        }],
        "initial_work_items": [{
            "work_item_key": "g1", "participant_key": "owner", "title": "Analyze request",
            "goal": "Produce the requested analysis", "acceptance_criteria": "Return a concise result",
        }],
        "suggested_edges": [],
    }


def _detect_step_id(messages: list[ChatMessage]) -> str | None:
    text = " ".join(m.content or "" for m in messages)
    match = re.search(r'Current step \((s\d+)\)|"node_key":"(s\d+)"', text)
    return next((value for value in match.groups() if value), None) if match else None


def _is_plan_generation(messages: list[ChatMessage]) -> bool:
    text = " ".join(m.content or "" for m in messages).lower()
    return ("generate a structured execution plan" in text or "generate a structured" in text
            or "generate a plan proposal as strict json" in text)


def _is_group_generation(messages: list[ChatMessage]) -> bool:
    text = " ".join(m.content or "" for m in messages).lower()
    return "generate a group formation proposal as strict json" in text


def _is_plan_revise(messages: list[ChatMessage]) -> bool:
    text = " ".join(m.content or "" for m in messages).lower()
    return "revised plan json" in text or "user instruction" in text


def _is_memory_extraction(messages: list[ChatMessage]) -> bool:
    return any(
        message.role == "system"
        and "Return strict JSON array only" in (message.content or "")
        and "fact_key" in (message.content or "")
        for message in messages
    )


def _build_content(messages: list[ChatMessage]) -> str:
    if _has_tool_result(messages):
        if any("sandbox-e2e" in (message.content or "") for message in messages if message.role == "tool"):
            return "Sandbox execution completed: sandbox-e2e"
        return "Sandbox Tool result processed successfully."
    text = " ".join(message.content or "" for message in messages).lower()
    if _is_memory_extraction(messages):
        return "[]"
    if _is_group_generation(messages):
        return json.dumps(_group_json(messages), ensure_ascii=False)
    if _is_plan_generation(messages):
        return json.dumps(_plan_json(messages), ensure_ascii=False)
    if _is_plan_revise(messages):
        revised = _plan_json(messages)
        return json.dumps(revised, ensure_ascii=False)
    if "produce the final plan delivery" in text:
        return "Final Plan delivery: all mock nodes settled successfully."
    if "produce the final grouprun delivery" in text:
        return "Final GroupRun delivery: all mock work settled successfully."
    if "execute this group work item" in text:
        return "Completed Group Work Item: mock collaboration result."
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


def _stream_tool_call(model: str, *, name: str, arguments: dict):
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    yield "data: " + json.dumps({
        "id": chunk_id, "object": "chat.completion.chunk", "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }) + "\n\n"
    yield "data: " + json.dumps({
        "id": chunk_id, "object": "chat.completion.chunk", "model": model,
        "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": f"call_mock_{name}_1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]}, "finish_reason": None}],
    }) + "\n\n"
    yield "data: " + json.dumps({
        "id": chunk_id, "object": "chat.completion.chunk", "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }) + "\n\n"
    yield "data: [DONE]\n\n"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest):
    if "slow runner restart acceptance" in " ".join(m.content or "" for m in body.messages).lower():
        await asyncio.sleep(5)
    control_tool = _requested_artifact_tool(body.messages, body.tools) or _requested_control_tool(body.messages, body.tools)
    if control_tool:
        name, arguments = control_tool
        tool_calls = [{
            "id": f"call_mock_{name}_1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]
        if body.stream:
            return StreamingResponse(
                _stream_tool_call(body.model, name=name, arguments=arguments),
                media_type="text/event-stream",
            )
        return _completion_payload("", body.model, tool_calls=tool_calls)
    if _should_execute_tool(body.messages, body.tools) and not _has_tool_result(body.messages):
        execute_arguments = _execute_arguments(body.messages)
        tool_calls = [{
            "id": "call_mock_execute_1", "type": "function",
            "function": {"name": "execute", "arguments": json.dumps(execute_arguments)},
        }]
        if body.stream:
            return StreamingResponse(
                _stream_tool_call(body.model, name="execute", arguments=execute_arguments),
                media_type="text/event-stream",
            )
        return _completion_payload("", body.model, tool_calls=tool_calls)
    content = _build_content(body.messages)
    if body.stream:
        return StreamingResponse(
            _stream_chunks(content, body.model),
            media_type="text/event-stream",
        )
    return _completion_payload(content, body.model)
