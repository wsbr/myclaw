"""E2E fixtures — extends integration fixtures."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

pytest_plugins = ["integration.conftest"]

from shared.api_client import MyClawClient


@pytest.fixture
def e2e_base_url() -> str:
    return os.environ.get("MYCLAW_E2E_URL", "http://test")


@pytest_asyncio.fixture
async def e2e_client(app_client, auth_headers, e2e_base_url):
    """
    In-process E2E client using ASGI transport.
    For Docker E2E, set MYCLAW_E2E_URL=http://localhost:8001 and use docker_client fixture.
    """
    from httpx import ASGITransport
    from app.main import app

    transport = ASGITransport(app=app)
    token = auth_headers["Authorization"].split(" ", 1)[1]
    async with MyClawClient("http://test", token=token) as client:
        client._client = app_client
        client._client.headers.update(auth_headers)
        yield client


@pytest_asyncio.fixture
async def docker_e2e_client():
    """HTTP client against docker-compose.test.yml api-test service."""
    base_url = os.environ.get("MYCLAW_E2E_URL", "http://localhost:8001")
    async with MyClawClient(base_url) as client:
        try:
            await client.health()
        except Exception as exc:
            pytest.skip(f"Docker E2E API not reachable at {base_url}: {exc}")
        await client.login(
            os.environ.get("INIT_ADMIN_EMAIL", "admin@test.myclaw.local"),
            os.environ.get("INIT_ADMIN_PASSWORD", "testpassword123"),
        )
        yield client
