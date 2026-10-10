"""Missing optional bindings must not create unreachable model instructions."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.runtime.agent_loop_coordinator import AgentLoopError
from app.workers.runtime_worker import _group_tool_rules, _require_working_plan_tool
from app.core.tool_registry.base import ToolResult
from app.services.runtime_resume_service import merge_pending_results, tool_continuation


def test_group_instructions_only_request_bound_coordination_tools():
    names = {"invoke_agent", "send_group_message", "search_group_history"}
    for bound in (set(), {"invoke_agent"}, {"search_group_history"}, names):
        rules = _group_tool_rules(bound)
        for name in names:
            assert (name in rules) == (name in bound)


@pytest.mark.parametrize("mode,permission,plan", [
    ("plan", "plan_readonly", None),
    ("chat", "direct", {"items": [{"text": "Check", "status": "pending"}]}),
    ("group", "direct", {"items": []}),
])
def test_required_plan_mutation_without_binding_fails_before_model(mode, permission, plan):
    with pytest.raises(AgentLoopError) as error:
        _require_working_plan_tool(
            mode=mode, permission_mode=permission,
            effective_tools=set(), working_plan=plan,
        )
    assert error.value.code == "working_plan_tool_unavailable"
    _require_working_plan_tool(
        mode=mode, permission_mode=permission,
        effective_tools={"update_plan"}, working_plan=plan,
    )


def test_normal_chat_and_completed_plan_do_not_require_plan_binding():
    for plan in (None, {"items": [{"text": "Check", "status": "completed"}]}):
        _require_working_plan_tool(
            mode="chat", permission_mode="direct",
            effective_tools=set(), working_plan=plan,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["ask_user", "invoke_agent"])
async def test_tool_wakeup_never_substitutes_runtime_ack_for_domain_answer(name):
    waiting = SimpleNamespace(
        provider_tool_call_id="human-call", tool_key=name, arguments={},
        authorization_snapshot={"runtime_durable_wait": True},
        result_data=ToolResult(status="succeeded", summary="Request accepted").model_dump(mode="json"),
    )
    ordinary = SimpleNamespace(
        provider_tool_call_id="file-call", tool_key="read_file", arguments={},
        authorization_snapshot={},
        result_data=ToolResult(status="succeeded", summary="File read").model_dump(mode="json"),
    )
    db = SimpleNamespace(scalars=AsyncMock(return_value=[waiting, ordinary]))
    calls = [{"id": item.provider_tool_call_id, "name": item.tool_key, "input": {}}
             for item in [waiting, ordinary]]
    pending = [{"role": "assistant", "content": "", "tool_calls": calls}]
    tool_results = await tool_continuation(db, work_item_id="work", source_turn_id="turn")
    merged = merge_pending_results(pending, tool_results)
    assert [item.get("tool_call_id") for item in merged if item["role"] == "tool"] == ["file-call"]
    domain_result = [{"role": "assistant", "tool_calls": [calls[0]]}, {
        "role": "tool", "tool_call_id": "human-call", "content": "The actual answer",
    }]
    merged = merge_pending_results(merged, domain_result)
    assert [item["content"] for item in merged if item.get("tool_call_id") == "human-call"] == ["The actual answer"]


@pytest.mark.asyncio
async def test_discovery_recovery_does_not_expose_internal_frozen_contracts():
    result = ToolResult(status="succeeded", summary="Catalog", data={
        "tools": [{"name": "read_file"}],
        "loaded_tool_definitions": [{"name": "read_file", "implementation_ref": "internal"}],
    }).model_dump(mode="json")
    invocation = SimpleNamespace(
        provider_tool_call_id="search-call", tool_key="search_tools", arguments={},
        authorization_snapshot={}, result_data=result,
    )
    db = SimpleNamespace(scalars=AsyncMock(return_value=[invocation]))
    messages = await tool_continuation(db, work_item_id="work", source_turn_id="turn")
    assert json.loads(messages[1]["content"])["data"] == {
        "tools": [{"name": "read_file"}], "loaded_tool_definitions": [],
    }
    assert result["data"]["loaded_tool_definitions"]
