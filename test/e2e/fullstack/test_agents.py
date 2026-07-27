"""Full-stack: Agent CRUD against deployed API."""

import uuid

import pytest

from e2e.fullstack.conftest import requires_fullstack
from shared.fullstack_helpers import seed_model_provider

pytestmark = [pytest.mark.fullstack, requires_fullstack]


@pytest.mark.asyncio
async def test_create_list_get_activate_agent(fs_client):
    provider = await seed_model_provider(fs_client)
    name = f"fs-agent-{uuid.uuid4().hex[:8]}"

    agent = await fs_client.create_agent(
        name=name,
        provider_id=provider["id"],
        modes=["chat", "plan"],
    )
    assert agent["status"] == "draft"

    agents = await fs_client.list_agents()
    assert any(a["id"] == agent["id"] for a in agents)

    detail = await fs_client.get_agent(agent["id"])
    assert detail["name"] == name

    activated = await fs_client.activate_agent(agent["id"])
    assert activated["status"] == "active"
