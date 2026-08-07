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
    return (
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
    if "idempotent Reporter summary Turn" in all_text:
        return None
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
        "ask clarification" in text
        or "request_clarification" in text
        or ("确认" in text and "统计口径" in text and "哪个周期" in text)
    ) and clarification_tool in names:
        return clarification_tool, {"question": "本轮退款差异应按第二季度还是六月单月统计？"}
    match = re.search(r"invoke(?:_| )agent(?: once)?(?: for agent)? ([0-9a-f-]{36})", text)
    if match and "invoke_agent" in names:
        return "invoke_agent", {"agent_id": match.group(1), "task": "Provide a secondary verification"}
    if (
        "订单风控复核员" in text and "独立复核" in text
    ) and "invoke_agent" in names:
        target_id = _catalog_agent_id_by_name(messages, "订单风控复核员")
        if target_id:
            return "invoke_agent", {
                "agent_id": target_id,
                "task": "独立复核同一账号短期内多笔退款，输出风险分层及证据依据。",
            }
    if (
        "search shared history" in text
        or "search_group_history" in text
        or ("本组" in text and "讨论记录" in text and "结论" in text)
    ) and "search_group_history" in names:
        return "search_group_history", {"query": "统计周期 重复退款", "limit": 20}
    return None


def _fixed_group_snapshot(messages: list[ChatMessage]) -> dict:
    marker = "Fixed GroupRun snapshot (data, not a workflow):"
    decoder = json.JSONDecoder()
    for message in reversed(messages):
        content = message.content or ""
        position = content.find(marker)
        if position < 0:
            continue
        start = content.find("{", position + len(marker))
        if start < 0:
            continue
        try:
            value, _end = decoder.raw_decode(content[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _group_member_id(snapshot: dict, name_marker: str) -> str | None:
    for member in snapshot.get("members") or []:
        boundary = member.get("capability_boundary") or {}
        if name_marker.casefold() in str(boundary.get("agent_name") or "").casefold():
            return member.get("agent_id")
    return None


def _group_member_id_with_tool(snapshot: dict, tool_key: str) -> str | None:
    for member in snapshot.get("members") or []:
        boundary = member.get("capability_boundary") or {}
        if tool_key in (boundary.get("tool_keys") or []):
            return member.get("agent_id")
    return None


def _current_group_member(snapshot: dict) -> dict:
    return next((item for item in snapshot.get("members") or [] if item.get("is_current")), {})


CAPACITY_PROGRAM = '''"""Deterministic capacity risk calculator."""
from __future__ import annotations

import argparse
import json


def emit(value, code=0):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    raise SystemExit(code)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--capacity", type=float)
    parser.add_argument("--committed", type=float)
    args = parser.parse_args()
    if args.committed is None:
        emit({"error": {"code": "missing_committed", "message": "--committed is required"}}, 2)
    if args.capacity is None or args.capacity <= 0:
        emit({"error": {"code": "invalid_capacity", "message": "total_capacity_hours must be greater than 0"}}, 2)
    if args.committed < 0:
        emit({"error": {"code": "invalid_committed", "message": "committed_hours must be greater than or equal to 0"}}, 2)
    capacity = int(args.capacity) if args.capacity.is_integer() else args.capacity
    committed = int(args.committed) if args.committed.is_integer() else args.committed
    utilization = round(committed / capacity * 100, 2)
    risk = "low" if utilization < 75 else "medium" if utilization <= 90 else "high"
    emit({
        "committed_hours": committed,
        "over_capacity_hours": max(committed - capacity, 0),
        "risk": risk,
        "total_capacity_hours": capacity,
        "utilization_percent": utilization,
    })
'''

CAPACITY_REQUIREMENTS = '''# Capacity risk requirements

utilization_percent = committed_hours / total_capacity_hours * 100
low: utilization < 75
medium: 75 <= utilization <= 90
high: utilization > 90
over_capacity_hours = max(committed_hours - total_capacity_hours, 0)
total_capacity_hours > 0
committed_hours >= 0
Cases: T01 T02 T03 T04 T05 T06 T07 T08 T09
'''

CAPACITY_TEST_REPORT = '''# Test report

Result: 9/9. For T01 T02 T03 T04 T05 T06 T07 T08 T09 the exact argv,
stdout, stderr, and exit code are recorded by the nine authoritative ToolAttempt rows.
'''

CAPACITY_ACCEPTANCE_REPORT = '''# Acceptance report

capacity_risk.v1: accepted, 9/9.
Artifacts: requirements.md, capacity_risk.py, test_report.md, acceptance_report.md.
'''

CAPACITY_CASE_CALLS = (
    (["python3", "capacity_risk.py", "--capacity", "120", "--committed", "138"], 'utilization_percent\\\":115.0'),
    (["python3", "capacity_risk.py", "--capacity", "100", "--committed", "74"], 'utilization_percent\\\":74.0'),
    (["python3", "capacity_risk.py", "--capacity", "100", "--committed", "75"], 'utilization_percent\\\":75.0'),
    (["python3", "capacity_risk.py", "--capacity", "100", "--committed", "90"], 'utilization_percent\\\":90.0'),
    (["python3", "capacity_risk.py", "--capacity", "100", "--committed", "91"], 'utilization_percent\\\":91.0'),
    (["python3", "capacity_risk.py", "--capacity", "100", "--committed", "0"], 'committed_hours\\\":0'),
    (["python3", "capacity_risk.py", "--capacity", "0", "--committed", "10"], "invalid_capacity"),
    (["python3", "capacity_risk.py", "--capacity", "100", "--committed", "-1"], "invalid_committed"),
    (["python3", "capacity_risk.py", "--capacity", "100"], "missing_committed"),
)


def _capacity_plan_tool(
    messages: list[ChatMessage], tools: list[dict] | None,
) -> tuple[str, dict] | None:
    """Exercise the same real Tool/Artifact path after Plan approval."""
    all_text = " ".join(message.content or "" for message in messages)
    user_text = " ".join(
        message.content or "" for message in messages if message.role == "user"
    )
    if "CAPACITY-RISK-PLAN-V2" not in user_text or '"mode": "direct"' not in all_text:
        return None
    names = _tool_names(tools)
    tool_results = " ".join(
        message.content or "" for message in messages if message.role == "tool"
    )
    compact_results = tool_results.replace(" ", "")
    if '"name":"acceptance_report.md"' in compact_results:
        return None
    for prior_name, artifact_name in (
        ("test_report.md", "acceptance_report.md"),
        ("capacity_risk.py", "test_report.md"),
        ("requirements.md", "capacity_risk.py"),
    ):
        if f'"name":"{prior_name}"' in compact_results and "publish_artifact" in names:
            return "publish_artifact", {
                "path": artifact_name, "name": artifact_name,
                "mime_type": (
                    "text/x-python" if artifact_name.endswith(".py")
                    else "text/markdown"
                ),
                "publish_to_group": False,
            }
    if '"path":"acceptance_report.md"' in compact_results and "publish_artifact" in names:
        return "publish_artifact", {
            "path": "requirements.md", "name": "requirements.md",
            "mime_type": "text/markdown", "publish_to_group": False,
        }
    if '"path":"test_report.md"' in compact_results and "write_file" in names:
        return "write_file", {
            "path": "acceptance_report.md", "content": CAPACITY_ACCEPTANCE_REPORT,
            "mode": "create",
        }
    for index in range(len(CAPACITY_CASE_CALLS) - 1, -1, -1):
        if CAPACITY_CASE_CALLS[index][1] not in tool_results:
            continue
        if index + 1 < len(CAPACITY_CASE_CALLS) and "execute" in names:
            return "execute", {
                "argv": CAPACITY_CASE_CALLS[index + 1][0],
                "cwd": ".", "timeout_seconds": 20,
            }
        if "write_file" in names:
            return "write_file", {
                "path": "test_report.md", "content": CAPACITY_TEST_REPORT,
                "mode": "create",
            }
    if '"path":"capacity_risk.py"' in compact_results and "execute" in names:
        return "execute", {
            "argv": CAPACITY_CASE_CALLS[0][0], "cwd": ".", "timeout_seconds": 20,
        }
    if '"path":"requirements.md"' in compact_results and "write_file" in names:
        return "write_file", {
            "path": "capacity_risk.py", "content": CAPACITY_PROGRAM,
            "mode": "create",
        }
    if "write_file" in names:
        return "write_file", {
            "path": "requirements.md", "content": CAPACITY_REQUIREMENTS,
            "mode": "create",
        }
    return None


def _capacity_group_tool(
    messages: list[ChatMessage], tools: list[dict] | None,
) -> tuple[str, dict] | None:
    all_text = " ".join(message.content or "" for message in messages)
    user_text = " ".join(
        message.content or "" for message in messages if message.role == "user"
    )
    if "CAPACITY-RISK-NETWORK-V2" not in user_text:
        return None
    if "idempotent Reporter summary Turn" in all_text:
        return None
    names = _tool_names(tools)
    snapshot = _fixed_group_snapshot(messages)
    current = _current_group_member(snapshot)
    boundary = current.get("capability_boundary") or {}
    current_name = str(boundary.get("agent_name") or "")
    current_request = _current_request_text(messages)
    tool_results = " ".join(
        message.content or "" for message in messages if message.role == "tool"
    )
    developer_id = _group_member_id(snapshot, "开发工程师")
    test_id = _group_member_id(snapshot, "测试工程师")
    product_id = str(snapshot.get("reporter_agent_id") or "") or None
    current_id = current.get("agent_id")

    if "HISTORY-CHECK" in current_request:
        if "search_group_history" in names and '"hits"' not in tool_results:
            return "search_group_history", {"query": "CAPACITY-RISK-NETWORK-V2", "limit": 50}
        return None

    if current_id == product_id:
        if "DELIVERY-READY" in current_request:
            return None
        if '"agent_invocation_id"' in tool_results:
            return None
        if (
            "send_group_message" in names and developer_id
            and '"message_id"' not in tool_results
            and "membership_policy_fixed_for_run" not in tool_results
        ):
            return "send_group_message", {
                "content": (
                    "CAPACITY-RISK-NETWORK-V2 CAPACITY-IMPLEMENT: write requirements.md and "
                    "capacity_risk.py, execute the exact T01-T09 contract, then write "
                    "test_report.md and acceptance_report.md, publish all four Group Artifacts, "
                    "and request Test review."
                ),
                "visibility": "direct",
                "recipient_agent_id": developer_id,
            }
        external = re.search(r"EXTERNAL-AGENT:([0-9a-f-]{36})", all_text)
        if (
            external and "invite_agent" in names
            and '"message_id"' in tool_results
            and "membership_policy_fixed_for_run" not in tool_results
        ):
            return "invite_agent", {
                "agent_id": external.group(1),
                "reason": "Capacity forecasting specialist might improve the calculation",
            }
        if (
            "membership_policy_fixed_for_run" in tool_results
            and "invoke_agent" in names and test_id
        ):
            return "invoke_agent", {
                "agent_id": test_id,
                "task": (
                    "CAPACITY-RISK-NETWORK-V2 CAPACITY-INVOKED-REVIEW: independently "
                    "review the proposed normal/high/critical acceptance boundaries."
                ),
                "input": {"thresholds": [0.85, 1.0]},
            }
        return None

    if current_id == developer_id:
        if "TEST-VERIFIED" in current_request:
            if "send_group_message" in names and product_id and '"message_id"' not in tool_results:
                return "send_group_message", {
                    "content": (
                        "CAPACITY-RISK-NETWORK-V2 DELIVERY-READY: program Artifact published, "
                        "self-tests and independent Test rerun passed; please perform final acceptance."
                    ),
                    "visibility": "direct",
                    "recipient_agent_id": product_id,
                }
            return None
        if "CAPACITY-IMPLEMENT" not in current_request:
            return None
        if '"message_id"' in tool_results:
            return None
        compact_results = tool_results.replace(" ", "")
        if (
            "send_group_message" in names and test_id
            and '"name":"acceptance_report.md"' in compact_results
        ):
            return "send_group_message", {
                "content": (
                    "CAPACITY-RISK-NETWORK-V2 TEST-REQUEST: independently read test_report.md; "
                    "the Runtime will verify nine ToolAttempt records and four Artifact checksums."
                ),
                "visibility": "direct",
                "recipient_agent_id": test_id,
            }
        if not tool_results and "write_file" in names:
            return "write_file", {
                "path": "requirements.md", "content": CAPACITY_REQUIREMENTS,
                "mode": "create",
            }
        if '"path":"requirements.md"' in compact_results and "write_file" in names:
            return "write_file", {
                "path": "capacity_risk.py", "content": CAPACITY_PROGRAM,
                "mode": "create",
            }
        if '"path":"capacity_risk.py"' in compact_results and "execute" in names:
            return "execute", {
                "argv": CAPACITY_CASE_CALLS[0][0], "cwd": ".", "timeout_seconds": 20,
            }
        for index, (_argv, marker) in enumerate(CAPACITY_CASE_CALLS):
            if marker not in tool_results:
                continue
            if index + 1 < len(CAPACITY_CASE_CALLS) and "execute" in names:
                return "execute", {
                    "argv": CAPACITY_CASE_CALLS[index + 1][0],
                    "cwd": ".", "timeout_seconds": 20,
                }
            if "write_file" in names:
                return "write_file", {
                    "path": "test_report.md", "content": CAPACITY_TEST_REPORT,
                    "mode": "create",
                }
        if '"path":"test_report.md"' in compact_results and "write_file" in names:
            return "write_file", {
                "path": "acceptance_report.md", "content": CAPACITY_ACCEPTANCE_REPORT,
                "mode": "create",
            }
        publish_transition = (
            ('"path":"acceptance_report.md"', "requirements.md"),
            ('"name":"requirements.md"', "capacity_risk.py"),
            ('"name":"capacity_risk.py"', "test_report.md"),
            ('"name":"test_report.md"', "acceptance_report.md"),
        )
        if "publish_artifact" in names:
            for prior_marker, artifact_name in publish_transition:
                if prior_marker in compact_results:
                    return "publish_artifact", {
                        "path": artifact_name, "name": artifact_name,
                        "mime_type": (
                            "text/x-python" if artifact_name.endswith(".py")
                            else "text/markdown"
                        ),
                        "publish_to_group": True,
                    }
        return None

    if current_id == test_id and "TEST-REQUEST" in current_request:
        if '"message_id"' in tool_results:
            return None
        if (
            "send_group_message" in names and developer_id
            and "9/9" in tool_results
        ):
            return "send_group_message", {
                "content": (
                    "CAPACITY-RISK-NETWORK-V2 TEST-VERIFIED: test_report.md was read; "
                    "deterministic Runtime Verification remains the acceptance authority."
                ),
                "visibility": "direct",
                "recipient_agent_id": developer_id,
            }
        if "read_file" in names:
            return "read_file", {"path": "test_report.md", "offset": 0, "limit": 65536}
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


def _is_memory_extraction(messages: list[ChatMessage]) -> bool:
    return any(
        message.role == "system"
        and "Return strict JSON array only" in (message.content or "")
        and "fact_key" in (message.content or "")
        for message in messages
    )


def _build_content(messages: list[ChatMessage]) -> str:
    all_text = " ".join(message.content or "" for message in messages)
    user_text = " ".join(
        message.content or "" for message in messages if message.role == "user"
    )
    if "CAPACITY-RISK-NETWORK-V2" in user_text:
        current = _current_request_text(messages)
        if "idempotent Reporter summary Turn" in all_text:
            return (
                "Capacity risk calculator delivered: Product acceptance completed, Development "
                "published four immutable Artifacts after nine exact executions, and Test reviewed "
                "the report. The fixed_for_run external invitation was rejected and audited."
            )
        if "DELIVERY-READY" in current:
            return "Product accepted the tested capacity risk calculator and its published Artifact."
        if "TEST-VERIFIED" in current:
            return "Development accepted the independent test evidence and returned delivery to Product."
        if "TEST-REQUEST" in current:
            return "Test completed an independent reverse review of the executable boundary suite."
        if "CAPACITY-INVOKED-REVIEW" in current:
            return "Test AgentInvocation review accepted the 0.85 high and 1.0 critical boundaries."
        if "CAPACITY-IMPLEMENT" in current:
            return "Development completed the program, self-tests, Artifact publication, and Test handoff."
        if "HISTORY-CHECK" in current:
            return "Repeated Group history query returned the persisted capacity collaboration facts."
    if _has_tool_result(messages):
        if any("sandbox-e2e" in (message.content or "") for message in messages if message.role == "tool"):
            return "Sandbox execution completed: sandbox-e2e"
        return "Sandbox Tool result processed successfully."
    text = " ".join(message.content or "" for message in messages).lower()
    if _is_memory_extraction(messages):
        return "[]"
    if "produce the final plan delivery" in text:
        return "Final Plan delivery: all mock nodes settled successfully."
    if "produce the final grouprun delivery" in text:
        return "Final GroupRun delivery: all mock work settled successfully."
    if "execute this group work item" in text:
        return "Completed Group Work Item: mock collaboration result."
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
    if (
        "slow runner restart acceptance" in request_text
        or ("退款账龄分布" in request_text and "约需数秒" in request_text)
    ):
        await asyncio.sleep(5)
    control_tool = (
        _capacity_plan_tool(body.messages, body.tools)
        or _capacity_group_tool(body.messages, body.tools)
        or _requested_working_plan_tool(body.messages, body.tools)
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
    content = _build_content(body.messages)
    if body.stream:
        return StreamingResponse(
            _stream_chunks(content, body.model),
            media_type="text/event-stream",
        )
    return _completion_payload(content, body.model)
