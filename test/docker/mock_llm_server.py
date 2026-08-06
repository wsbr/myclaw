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
    tool_choice: str | dict | None = None
    response_format: dict | None = None


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


def _forced_tool_name(tool_choice: str | dict | None) -> str | None:
    if not isinstance(tool_choice, dict):
        return None
    function = tool_choice.get("function") or {}
    return tool_choice.get("name") or function.get("name")


def _v2_conformance_call(body: ChatCompletionRequest) -> tuple[str, dict] | None:
    forced = _forced_tool_name(body.tool_choice)
    names = _tool_names(body.tools)
    text = " ".join(message.content or "" for message in body.messages)
    tool_results = " ".join(
        message.content or "" for message in body.messages if message.role == "tool"
    )
    if forced in {"update_plan", "ask_user", "invoke_agent"}:
        assert names == {"update_plan", "ask_user", "invoke_agent"}
        arguments = {
            "update_plan": {"revision": 2},
            "ask_user": {"question": "Which region should be used?"},
            "invoke_agent": {
                "agent_id": "agent-conformance", "request": "Review the evidence.",
            },
        }
        return forced, arguments[forced]
    if forced != "myclaw_conformance_echo":
        return None
    if "tool_arguments_schema_error" in tool_results:
        return forced, {"value": "repaired"}
    if _has_tool_result(body.messages):
        return None
    value = "native-v2-repeat" if "native-v2-repeat" in text else "native-v2"
    return forced, {"value": value}


def _v2_goal1_smoke_call(body: ChatCompletionRequest) -> tuple[str, dict] | None:
    names = _tool_names(body.tools)
    text = " ".join(message.content or "" for message in body.messages)
    if "goal1-v2-fullstack-smoke" not in text or _has_tool_result(body.messages):
        return None
    if "write_file" not in names:
        return None
    return "write_file", {
        "path": "goal1-v2-smoke.txt",
        "content": "goal1-v2-fullstack-smoke",
        "mode": "create",
    }


def _pending_effect(messages: list[ChatMessage], tool_key: str) -> bool:
    marker = json.dumps(tool_key)
    return any(
        message.role == "system"
        and "Machine-owned Completion Contract" in (message.content or "")
        and marker in (message.content or "").split("pending_effects", 1)[-1]
        for message in messages
    )


def _current_request_text(messages: list[ChatMessage]) -> str:
    platform_continuations = (
        "the user approved workingplan",
        "the user rejected workingplan",
        "the user did not approve the current plan",
    )
    for message in reversed(messages):
        content = (message.content or "").strip()
        if message.role != "user" or not content:
            continue
        if content.lower().startswith(platform_continuations):
            continue
        return content
    return ""


def _should_execute_tool(messages: list[ChatMessage], tools: list[dict] | None) -> bool:
    if "execute" not in _tool_names(tools):
        return False
    user_msgs = [m.content for m in messages if m.role == "user" and m.content]
    if not user_msgs:
        return False
    text = user_msgs[-1].lower()
    return _pending_effect(messages, "execute") or (
        "sandbox command" in text
        or ("隔离分析环境" in text and ("退款" in text or "校验探针" in text))
    )


def _execute_arguments(messages: list[ChatMessage]) -> dict:
    user_msgs = [m.content or "" for m in messages if m.role == "user"]
    if user_msgs and (
        "sandbox file" in user_msgs[-1].lower() or "acceptance.txt" in user_msgs[-1].lower()
    ):
        return {
            "argv": [
                "python", "-c",
                "from pathlib import Path; p=Path('acceptance.txt'); p.write_text('sandbox-e2e'); print(p.read_text())",
            ],
            "timeout_seconds": 5,
        }
    return {"argv": ["/bin/echo", "sandbox-e2e"], "timeout_seconds": 5}


def _requested_control_tool(messages: list[ChatMessage], tools: list[dict] | None) -> tuple[str, dict] | None:
    names = _tool_names(tools)
    text = _current_request_text(messages).lower()
    tool_results = " ".join(
        message.content or "" for message in messages if message.role == "tool"
    )
    all_text = " ".join(message.content or "" for message in messages)
    collaboration_settled = (
        '"agent_invocation_id"' in tool_results
        or "agentinvocation " in all_text.lower()
    )
    if (
        "ask clarification" in text
        and '"clarification_id"' not in tool_results
        and "采用 2026 年第二季度口径" not in all_text
        and "ask_user" in names
    ):
        return "ask_user", {
            "question": "本轮复核应采用第二季度还是六月单月统计口径？",
        }
    if "plan-agent-network" in text:
        role_queries = {
            "plan-agent-network-product": "产品经理",
            "plan-agent-network-development": "开发工程师",
            "plan-agent-network-test": "测试工程师",
        }
        role_query = next(
            (value for marker, value in role_queries.items() if marker in text),
            None,
        )
        if (
            '"candidates"' not in tool_results
            and not collaboration_settled
            and "discover_agents" in names
        ):
            return "discover_agents", {
                **({"query": role_query} if role_query else {}),
                "limit": 10,
            }
        if (
            '"candidates"' in tool_results
            and not collaboration_settled
            and "invoke_agent" in names
        ):
            match = re.search(r'"agent_id"\s*:\s*"([0-9a-f-]{36})"', tool_results)
            if match:
                return "invoke_agent", {
                    "agent_id": match.group(1),
                    "task": "Perform a bounded read-only specialist review of the proposed rollout.",
                    "input": {"focus": "risk and test evidence"},
                }
    if _has_tool_result(messages):
        return None
    clarification_tool = "ask_user" if "ask_user" in names else "request_clarification"
    if (
        _pending_effect(messages, "request_clarification")
        or
        "ask clarification" in text
        or "request_clarification" in text
        or ("确认" in text and "统计口径" in text and "哪个周期" in text)
    ) and clarification_tool in names:
        return clarification_tool, {"question": "本轮退款差异应按第二季度还是六月单月统计？"}
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
    if (
        _pending_effect(messages, "invoke_agent")
        or ("订单风控复核员" in text and "独立复核" in text)
    ) and "invoke_agent" in names:
        target_id = _catalog_agent_id_by_name(messages, "订单风控复核员")
        if target_id:
            return "invoke_agent", {
                "agent_id": target_id,
                "task": "独立复核同一账号短期内多笔退款，输出风险分层及证据依据。",
            }
    if (
        _pending_effect(messages, "search_group_history")
        or
        "search shared history" in text
        or "search_group_history" in text
        or ("本组" in text and "讨论记录" in text and "结论" in text)
    ) and "search_group_history" in names:
        return "search_group_history", {"query": "统计周期 重复退款", "limit": 20}
    return None


def _requested_artifact_tool(messages: list[ChatMessage], tools: list[dict] | None) -> tuple[str, dict] | None:
    names = _tool_names(tools)
    user_text = _current_request_text(messages).lower()
    all_text = " ".join(message.content or "" for message in messages)
    if (
        "publish acceptance artifact" not in user_text
        and not ("可下载" in user_text and "验收报告 2026.txt" in user_text)
    ):
        return None
    if '"mode": "direct"' not in all_text:
        return None
    tool_results = " ".join(m.content or "" for m in messages if m.role == "tool")
    if "验收报告 2026.txt" not in tool_results and "write_file" in names:
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


def _requested_working_plan_tool(
    messages: list[ChatMessage], tools: list[dict] | None,
) -> tuple[str, dict] | None:
    names = _tool_names(tools)
    all_text = " ".join(message.content or "" for message in messages)
    if (
        "update_plan" not in names or _has_tool_result(messages)
        or ("plan_readonly" not in all_text and "WorkingPlan:" in all_text)
    ):
        return None
    text = _current_request_text(messages).lower()
    if not any(marker in text for marker in ("复杂任务", "分步骤", "multi-step", "workingplan")):
        return None
    return "update_plan", {
        "summary": "完成复杂任务的分步交付",
        "items": [
            {"text": "确认目标与输入", "status": "completed"},
            {"text": "执行分析并整理结果", "status": "in_progress"},
            {"text": "交付并核验结果", "status": "pending"},
        ],
        "explanation": "任务包含多个相互依赖的步骤。",
    }


def _has_tool_result(messages: list[ChatMessage]) -> bool:
    return any(m.role == "tool" for m in messages)


def _catalog_agent_id(messages: list[ChatMessage], flag: str) -> str:
    text = " ".join(message.content or "" for message in messages)
    match = re.search(rf'"agent_id":"([^"]+)"[^}}]*"{flag}":true', text)
    if match:
        return match.group(1)
    match = re.search(r'"agent_id":"([^"]+)"', text)
    return match.group(1) if match else "00000000-0000-0000-0000-000000000001"


def _catalog_agent_id_by_name(messages: list[ChatMessage], name: str) -> str | None:
    text = " ".join(message.content or "" for message in messages)
    match = re.search(
        rf'"agent_id":"([0-9a-f-]{{36}})","name":"{re.escape(name)}[^" ]*',
        text,
    )
    return match.group(1) if match else None


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


def _is_completion_contract_planning(messages: list[ChatMessage]) -> bool:
    return any(
        message.role == "system"
        and "machine-owned completion-contract planner" in (message.content or "").lower()
        for message in messages
    )


def _completion_intent_json(messages: list[ChatMessage]) -> dict:
    user_messages = [message.content or "" for message in messages if message.role == "user"]
    try:
        request = json.loads(user_messages[-1]) if user_messages else {}
    except json.JSONDecodeError:
        request = {}
    text = str(request.get("goal") or (user_messages[-1] if user_messages else "")).lower()
    required: list[str] = []

    def require(key: str, condition: bool) -> None:
        if condition and key not in required:
            required.append(key)

    require("request_clarification", (
        "先向我确认" in text
        or ("确认" in text and "哪个周期" in text)
        or "ask me" in text and "clarif" in text
    ))
    require("invoke_agent", (
        ("订单风控复核员" in text and "独立复核" in text)
        or "another agent" in text and "independent" in text
    ))
    require("search_group_history", (
        ("本组" in text and "讨论记录" in text and ("找出" in text or "检索" in text))
        or "group history" in text
    ))
    require("execute", (
        "隔离分析环境运行" in text
        or "run python" in text
        or "sandbox command" in text
    ))
    file_delivery = (
        "可下载" in text
        or "正式发布" in text
        or "publish" in text and "artifact" in text
    )
    require("write_file", file_delivery and ("文件" in text or ".txt" in text or "artifact" in text))
    require("publish_artifact", file_delivery)
    require("read_file", "读取现有文件" in text or "read existing file" in text)

    # Keep unavailable requested effects visible so the Runtime can report a
    # stable missing-capability error instead of accepting a simulated action.
    if len(required) > 1:
        reason = "multiple_effects_required"
    elif required == ["request_clarification"]:
        reason = "clarification_required"
    elif required == ["invoke_agent"]:
        reason = "delegated_review_required"
    elif required == ["search_group_history"]:
        reason = "group_evidence_required"
    elif required == ["execute"]:
        reason = "sandbox_execution_required"
    elif required == ["read_file"]:
        reason = "existing_file_required"
    elif required:
        reason = "file_delivery_required"
    else:
        reason = "no_observable_effect"
    return {"required_tool_keys": required, "reason_code": reason}


def _is_completion_turn(messages: list[ChatMessage]) -> bool:
    return any(
        message.role == "system"
        and "Machine-owned Completion Contract" in (message.content or "")
        for message in messages
    )


def _completion_turn_payload(messages: list[ChatMessage], content: str) -> dict:
    declared: list[str] = []
    for message in reversed(messages):
        if message.role != "system" or "Machine-owned Completion Contract" not in (message.content or ""):
            continue
        match = re.search(r'"required_effects":(\[[^\]]*\])', message.content or "")
        if match:
            try:
                declared = json.loads(match.group(1))
            except json.JSONDecodeError:
                declared = []
        break
    inferred = _completion_intent_json(messages)
    required = declared or inferred["required_tool_keys"]
    if required == inferred["required_tool_keys"]:
        reason = inferred["reason_code"]
    elif len(required) > 1:
        reason = "multiple_effects_required"
    elif required == ["request_clarification"]:
        reason = "clarification_required"
    elif required == ["invoke_agent"]:
        reason = "delegated_review_required"
    elif required == ["search_group_history"]:
        reason = "group_evidence_required"
    elif required == ["execute"]:
        reason = "sandbox_execution_required"
    elif required == ["read_file"]:
        reason = "existing_file_required"
    else:
        reason = "file_delivery_required" if required else "no_observable_effect"
    return {"message": content, "required_tool_keys": required, "reason_code": reason}


def _simulate_first_invalid_completion(messages: list[ChatMessage]) -> bool:
    user_text = " ".join(
        message.content or "" for message in messages if message.role == "user"
    )
    system_text = " ".join(
        message.content or "" for message in messages if message.role == "system"
    )
    return "本次只需回复确认" in user_text and '"repair_no":0' in system_text


def _simulate_repeated_completed_search(
    messages: list[ChatMessage], tools: list[dict] | None,
) -> bool:
    """Reproduce a real provider hallucinating an already removed Tool once."""
    names = _tool_names(tools)
    system_text = " ".join(
        message.content or "" for message in messages if message.role == "system"
    )
    return (
        "finalize_response" in names
        and "search_group_history" not in names
        and '"required_effects":["search_group_history"]' in system_text
        and '"satisfied_effects":["search_group_history"]' in system_text
        and '"repair_no":0' in system_text
    )


def _simulate_declared_clarification(
    messages: list[ChatMessage], tools: list[dict] | None,
) -> bool:
    """Declare a clarification without calling its native Tool, as some providers do."""
    names = _tool_names(tools)
    text = " ".join(message.content or "" for message in messages)
    return (
        "目前业务材料同时出现" in text
        and {"finalize_response", "request_clarification"} <= names
        and '"repair_no":0' in text
    )


def _simulate_plain_after_settled_search(
    messages: list[ChatMessage], tools: list[dict] | None,
) -> bool:
    names = _tool_names(tools)
    text = " ".join(message.content or "" for message in messages if message.role == "system")
    return (
        "finalize_response" in names
        and '"required_effects":["search_group_history"]' in text
        and '"satisfied_effects":["search_group_history"]' in text
        and '"repair_no":1' in text
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
    yield "data: " + json.dumps({
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }) + "\n\n"
    yield "data: [DONE]\n\n"


def _stream_tool_call(model: str, *, name: str, arguments: dict):
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    call_id = f"call_mock_{name}_{uuid.uuid4().hex[:8]}"
    yield "data: " + json.dumps({
        "id": chunk_id, "object": "chat.completion.chunk", "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }) + "\n\n"
    yield "data: " + json.dumps({
        "id": chunk_id, "object": "chat.completion.chunk", "model": model,
        "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]}, "finish_reason": None}],
    }) + "\n\n"
    yield "data: " + json.dumps({
        "id": chunk_id, "object": "chat.completion.chunk", "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }) + "\n\n"
    yield "data: " + json.dumps({
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }) + "\n\n"
    yield "data: [DONE]\n\n"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest):
    request_text = " ".join(m.content or "" for m in body.messages).lower()
    conformance_call = _v2_conformance_call(body)
    if conformance_call:
        name, arguments = conformance_call
        if body.stream:
            return StreamingResponse(
                _stream_tool_call(body.model, name=name, arguments=arguments),
                media_type="text/event-stream",
            )
        return _completion_payload("", body.model, tool_calls=[{
            "id": f"call_mock_{name}_1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }])
    if (
        "goal1-v2-fullstack-smoke" in request_text
        and _has_tool_result(body.messages)
    ):
        visible = "write_file" in _tool_names(body.tools)
        content = (
            "Agent-native v2 full-stack smoke passed."
            if visible else "ERROR: write_file disappeared after use."
        )
        if body.stream:
            return StreamingResponse(
                _stream_chunks(content, body.model), media_type="text/event-stream",
            )
        return _completion_payload(content, body.model)
    smoke_call = _v2_goal1_smoke_call(body)
    if smoke_call:
        name, arguments = smoke_call
        if body.stream:
            return StreamingResponse(
                _stream_tool_call(body.model, name=name, arguments=arguments),
                media_type="text/event-stream",
            )
        return _completion_payload("", body.model, tool_calls=[{
            "id": f"call_mock_{name}_goal1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }])
    if _is_completion_contract_planning(body.messages):
        return _completion_payload(
            json.dumps(_completion_intent_json(body.messages), ensure_ascii=False),
            body.model,
        )
    if (
        "slow runner restart acceptance" in request_text
        or ("退款账龄分布" in request_text and "约需数秒" in request_text)
    ):
        await asyncio.sleep(5)
    if _simulate_declared_clarification(body.messages, body.tools):
        arguments = {
            "message": "本轮退款差异应按第二季度还是六月单月统计？",
            "required_tool_keys": ["request_clarification"],
            "reason_code": "clarification_required",
        }
        declared_content = json.dumps(arguments, ensure_ascii=False)
        if body.stream:
            return StreamingResponse(
                _stream_chunks(declared_content, body.model),
                media_type="text/event-stream",
            )
        return _completion_payload(declared_content, body.model)
    control_tool = (
        _requested_working_plan_tool(body.messages, body.tools)
        or _requested_artifact_tool(body.messages, body.tools)
        or _requested_control_tool(body.messages, body.tools)
    )
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
    if _simulate_repeated_completed_search(body.messages, body.tools):
        arguments = {"query": "统计周期 重复退款", "limit": 20}
        if body.stream:
            return StreamingResponse(
                _stream_tool_call(
                    body.model, name="search_group_history", arguments=arguments,
                ),
                media_type="text/event-stream",
            )
        return _completion_payload("", body.model, tool_calls=[{
            "id": "call_mock_repeated_search_group_history_1", "type": "function",
            "function": {
                "name": "search_group_history",
                "arguments": json.dumps(arguments),
            },
        }])
    if _simulate_plain_after_settled_search(body.messages, body.tools):
        content = "已基于本组讨论记录完成可追溯复核摘要。"
        if body.stream:
            return StreamingResponse(
                _stream_chunks(content, body.model), media_type="text/event-stream",
            )
        return _completion_payload(content, body.model)
    content = _build_content(body.messages)
    if "finalize_response" in _tool_names(body.tools):
        arguments = _completion_turn_payload(body.messages, content)
        if _simulate_first_invalid_completion(body.messages):
            arguments = {"message": content}
        if body.stream:
            return StreamingResponse(
                _stream_tool_call(body.model, name="finalize_response", arguments=arguments),
                media_type="text/event-stream",
            )
        return _completion_payload("", body.model, tool_calls=[{
            "id": "call_mock_finalize_response_1", "type": "function",
            "function": {"name": "finalize_response", "arguments": json.dumps(arguments)},
        }])
    if body.stream:
        return StreamingResponse(
            _stream_chunks(content, body.model),
            media_type="text/event-stream",
        )
    return _completion_payload(content, body.model)
