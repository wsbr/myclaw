"""Integration tests for Agent assembly (tools/skills/mcp/builtin) and runtime effect."""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _assemble_via_api(app_client, auth_headers) -> dict:
    provider_resp = await app_client.post(
        "/api/v1/model-providers",
        headers=auth_headers,
        json={
            "name": f"asm-provider-{uuid.uuid4().hex[:6]}",
            "provider": "openai_compatible",
            "base_url": "http://mock-llm:9090/v1",
            "api_key": "sk-asm",
        },
    )
    assert provider_resp.status_code == 201
    provider = provider_resp.json()
    await app_client.patch(
        f"/api/v1/model-providers/{provider['id']}/activate",
        headers=auth_headers,
    )

    agent_resp = await app_client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": f"asm-agent-{uuid.uuid4().hex[:6]}",
            "description": "draft",
            "modes": ["chat"],
            "model_config_": {"provider_id": provider["id"], "model_id": "mock-model"},
            "system_prompt_blocks": {},
        },
    )
    assert agent_resp.status_code == 201
    agent = agent_resp.json()
    assert agent["version"] == 1

    update_resp = await app_client.put(
        f"/api/v1/agents/{agent['id']}",
        headers=auth_headers,
        json={
            "description": "Fully assembled integration agent",
            "system_prompt_blocks": {
                "identity": "You are a study assistant.",
                "instructions": "Use builtin tools for reminders.",
            },
            "model_config_": {
                "provider_id": provider["id"],
                "model_id": "mock-model",
                "temperature": 0.2,
            },
            "context_strategy": {"window_type": "sliding", "max_tokens": 32000},
        },
    )
    assert update_resp.status_code == 200
    agent = update_resp.json()
    assert agent["version"] == 2

    tool_resp = await app_client.post(
        "/api/v1/tools",
        headers=auth_headers,
        json={
            "name": f"asm_tool_{uuid.uuid4().hex[:6]}",
            "display_name": "ASM Tool",
            "description": "Test tool",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    )
    assert tool_resp.status_code == 201
    tool = tool_resp.json()

    skill_resp = await app_client.post(
        "/api/v1/skills",
        headers=auth_headers,
        json={
            "name": f"asm_skill_{uuid.uuid4().hex[:6]}",
            "display_name": "BFS Study Skill",
            "description": "BFS guide",
            "content": "Breadth-first search traverses graphs level by level.",
        },
    )
    assert skill_resp.status_code == 201
    skill = skill_resp.json()

    mcp_resp = await app_client.post(
        "/api/v1/mcp-servers",
        headers=auth_headers,
        json={
            "name": f"asm_mcp_{uuid.uuid4().hex[:6]}",
            "display_name": "ASM MCP",
            "description": "test",
            "endpoint": "http://localhost:9999/mcp",
        },
    )
    assert mcp_resp.status_code == 201
    mcp = mcp_resp.json()

    tools_put = await app_client.put(
        f"/api/v1/agents/{agent['id']}/tools",
        headers=auth_headers,
        json={
            "tool_ids": [tool["id"]],
            "builtin_tools": {"create_scheduled_task": True},
        },
    )
    assert tools_put.status_code == 200

    skills_put = await app_client.put(
        f"/api/v1/agents/{agent['id']}/skills",
        headers=auth_headers,
        json={"skill_ids": [skill["id"]]},
    )
    assert skills_put.status_code == 200

    mcp_put = await app_client.put(
        f"/api/v1/agents/{agent['id']}/mcp-servers",
        headers=auth_headers,
        json={"mcp_server_ids": [mcp["id"]]},
    )
    assert mcp_put.status_code == 200

    activate_resp = await app_client.post(
        f"/api/v1/agents/{agent['id']}/activate",
        headers=auth_headers,
    )
    assert activate_resp.status_code == 200
    activated = activate_resp.json()
    assert activated["status"] == "active"

    versions_resp = await app_client.get(
        f"/api/v1/agents/{agent['id']}/versions",
        headers=auth_headers,
    )
    versions = versions_resp.json()

    return {
        "agent": activated,
        "provider": provider,
        "tool": tool,
        "skill": skill,
        "mcp": mcp,
        "versions": versions,
    }


async def test_agent_assembly_persists_config_and_versions(app_client, auth_headers):
    data = await _assemble_via_api(app_client, auth_headers)
    agent = data["agent"]

    detail = await app_client.get(f"/api/v1/agents/{agent['id']}", headers=auth_headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["description"] == "Fully assembled integration agent"
    assert body["system_prompt_blocks"]["identity"] == "You are a study assistant."
    assert body["version"] == 2
    assert len(data["versions"]) >= 2


async def test_assembled_agent_invokes_scheduled_task_tool(
    app_client, auth_headers, mock_model_hub, celery_eager
):
    data = await _assemble_via_api(app_client, auth_headers)
    agent = data["agent"]

    session_resp = await app_client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        json={"agent_id": agent["id"], "mode": "chat"},
    )
    assert session_resp.status_code == 201
    session = session_resp.json()

    chat_resp = await app_client.post(
        f"/api/v1/sessions/{session['id']}/chat",
        headers=auth_headers,
        json={"message": "5分钟后提醒我复习BFS"},
    )
    assert chat_resp.status_code == 200
    assert "tool_result" in chat_resp.text or "create_scheduled_task" in chat_resp.text

    tasks_resp = await app_client.get("/api/v1/tasks", headers=auth_headers)
    assert tasks_resp.status_code == 200
    tasks = tasks_resp.json()
    scheduled = [
        t for t in tasks
        if t.get("trigger_type") == "scheduled" and t.get("title") == "复习提醒"
    ]
    assert len(scheduled) >= 1
    assert scheduled[0]["agent_id"] == agent["id"]

    trace_resp = await app_client.get(
        f"/api/v1/sessions/{session['id']}/runners",
        headers=auth_headers,
    )
    runners = trace_resp.json()
    assert len(runners) >= 1

    spans_resp = await app_client.get(
        f"/api/v1/sessions/{session['id']}/runners/{runners[0]['id']}/spans",
        headers=auth_headers,
    )
    spans = spans_resp.json()
    tool_spans = [s for s in spans if s.get("span_type") == "tool_call"]
    assert any("create_scheduled_task" in (s.get("name") or "") for s in tool_spans)

    assert any(
        "BFS Study Skill" in call.get("params", {}).get("system", "")
        for call in mock_model_hub.call_log
        if call.get("mode") == "chat"
    )
