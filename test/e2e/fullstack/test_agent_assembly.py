"""Full-stack: Agent assembly and runtime builtin tool invocation."""

import pytest

from e2e.fullstack.conftest import requires_fullstack
from shared.fullstack_helpers import assemble_agent

pytestmark = [pytest.mark.fullstack, requires_fullstack]


@pytest.mark.asyncio
async def test_full_assembly_creates_versioned_agent(fs_client):
    assembled = await assemble_agent(fs_client)

    assert assembled["status"] == "active"
    assert assembled["version"] >= 2
    assert len(assembled["versions"]) >= 2

    detail = await fs_client.get_agent(assembled["id"])
    assert detail["system_prompt_blocks"]["identity"] == "You are a study assistant."
    assert detail["model_config_"]["temperature"] == 0.3


@pytest.mark.asyncio
async def test_assembled_agent_chat_triggers_scheduled_task(fs_client):
    assembled = await assemble_agent(fs_client)
    session = await fs_client.create_session(assembled["id"], mode="chat")

    response = await fs_client.chat(session["id"], "5分钟后提醒我复习BFS")
    assert "定时提醒" in response or "复习" in response

    tasks = await fs_client.list_tasks()
    scheduled = [
        t for t in tasks
        if t.get("trigger_type") == "scheduled"
        and t.get("title") == "复习提醒"
        and t.get("agent_id") == assembled["id"]
    ]
    assert len(scheduled) == 1
    assert scheduled[0]["status"] == "pending"

    trace = await fs_client.fetch_full_trace(session["id"])
    all_spans = [s for spans in trace["spans_by_runner"].values() for s in spans]
    tool_spans = [s for s in all_spans if s.get("span_type") == "tool_call"]
    assert any("create_scheduled_task" in (s.get("name") or "") for s in tool_spans)
