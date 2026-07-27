"""Full-stack: Admin resource APIs (users, tools, skills, mcp, models)."""

import uuid

import pytest

from e2e.fullstack.conftest import requires_fullstack

pytestmark = [pytest.mark.fullstack, requires_fullstack]


@pytest.mark.asyncio
async def test_list_tools_api(fs_client):
    tools = await fs_client.list_tools()
    assert isinstance(tools, list)
    # Builtin tools live in code registry; DB tools table may be empty on fresh install


@pytest.mark.asyncio
async def test_list_skills(fs_client):
    skills = await fs_client.list_skills()
    assert isinstance(skills, list)


@pytest.mark.asyncio
async def test_list_mcp_servers(fs_client):
    servers = await fs_client.list_mcp_servers()
    assert isinstance(servers, list)


@pytest.mark.asyncio
async def test_list_model_providers(fs_client):
    providers = await fs_client.list_model_providers()
    assert isinstance(providers, list)


@pytest.mark.asyncio
async def test_admin_user_crud(fs_client):
    email = f"testuser-{uuid.uuid4().hex[:6]}@test.myclaw.local"
    user = await fs_client.create_admin_user(
        email=email,
        username=f"user-{uuid.uuid4().hex[:6]}",
        password="testpass123",
        role="user",
    )
    assert user["email"] == email

    users = await fs_client.list_admin_users()
    assert any(u["email"] == email for u in users)
