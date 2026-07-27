"""Helpers for full-stack deployment tests."""

from __future__ import annotations

import os
import uuid

from shared.api_client import MyClawClient

MOCK_LLM_URL = os.environ.get("MYCLAW_MOCK_LLM_URL", "http://localhost:9090")
# URL stored in model provider — must be reachable from api/worker containers inside Docker network
MOCK_LLM_INTERNAL_URL = os.environ.get("MYCLAW_MOCK_LLM_INTERNAL_URL", "http://mock-llm:9090")
EXPECTED_PLAN_STEPS = 2


async def seed_model_provider(client: MyClawClient, name: str | None = None) -> dict:
    provider = await client.create_model_provider(
        name=name or f"fs-provider-{uuid.uuid4().hex[:8]}",
        provider="openai_compatible",
        base_url=f"{MOCK_LLM_INTERNAL_URL}/v1",
        api_key="sk-fullstack-test",
    )
    await client.activate_model_provider(provider["id"])
    return provider


async def seed_agent(
    client: MyClawClient,
    provider_id: str,
    *,
    name: str | None = None,
    modes: list[str] | None = None,
) -> dict:
    agent = await client.create_agent(
        name=name or f"fs-agent-{uuid.uuid4().hex[:8]}",
        provider_id=provider_id,
        modes=modes or ["chat", "plan"],
    )
    await client.activate_agent(agent["id"])
    return agent


async def seed_ready_agent(
    client: MyClawClient,
    modes: list[str] | None = None,
) -> dict:
    provider = await seed_model_provider(client)
    agent = await seed_agent(client, provider["id"], modes=modes)
    return {**agent, "provider_id": provider["id"]}


async def assemble_agent(
    client: MyClawClient,
    *,
    name: str | None = None,
    enable_scheduled_task: bool = True,
) -> dict:
    """Full assembly flow: provider → agent → resources → tools/skills/mcp → activate."""
    import uuid

    provider = await seed_model_provider(client)
    agent_name = name or f"assembled-agent-{uuid.uuid4().hex[:8]}"

    agent = await client.create_agent(
        name=agent_name,
        provider_id=provider["id"],
        modes=["chat"],
    )
    agent = await client.update_agent(agent["id"], {
        "description": "Fully assembled test agent",
        "system_prompt_blocks": {
            "identity": "You are a study assistant.",
            "instructions": "Use builtin tools when users ask for reminders.",
            "context": "Corporate learning platform.",
            "output_format": "Concise Chinese.",
        },
        "model_config_": {
            "provider_id": provider["id"],
            "model_id": "mock-model",
            "temperature": 0.3,
            "max_tokens": 4096,
        },
        "context_strategy": {"window_type": "sliding", "max_tokens": 32000, "compression_threshold": 0.8},
        "memory_config": {"short_term": True, "long_term": {"enabled": False}},
        "supported_channels": ["platform_ui"],
    })

    tool = await client.create_tool(
        name=f"test_tool_{uuid.uuid4().hex[:6]}",
        display_name="Test Lookup Tool",
        description="Returns static lookup data",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    skill = await client.create_skill(
        name=f"test_skill_{uuid.uuid4().hex[:6]}",
        display_name="BFS Study Skill",
        description="BFS algorithm study guide",
        content="Use breadth-first search to traverse graphs level by level.",
    )
    mcp = await client.create_mcp_server(
        name=f"test_mcp_{uuid.uuid4().hex[:6]}",
        display_name="Test MCP",
        endpoint="http://localhost:9999/mcp",
    )

    builtin_tools: dict[str, bool] = {}
    if enable_scheduled_task:
        builtin_tools["create_scheduled_task"] = True

    await client.update_agent_tools(agent["id"], [tool["id"]], builtin_tools)
    await client.update_agent_skills(agent["id"], [skill["id"]])
    await client.update_agent_mcp_servers(agent["id"], [mcp["id"]])

    activated = await client.activate_agent(agent["id"])
    versions = await client.list_agent_versions(agent["id"])

    return {
        **activated,
        "provider_id": provider["id"],
        "tool_id": tool["id"],
        "skill_id": skill["id"],
        "mcp_server_id": mcp["id"],
        "versions": versions,
    }
