"""Fixtures for full-stack deployment tests (real Docker stack)."""

from __future__ import annotations

import os

import httpx
import pytest
import pytest_asyncio

from shared.api_client import MyClawClient

FULLSTACK_API_URL = os.environ.get("MYCLAW_FULLSTACK_API_URL", "http://localhost:8001")
FULLSTACK_UI_URL = os.environ.get("MYCLAW_FULLSTACK_UI_URL", "http://localhost:8081")


def _stack_reachable() -> bool:
    try:
        with httpx.Client(timeout=3.0) as c:
            api_ok = c.get(f"{FULLSTACK_API_URL}/health").status_code == 200
            ui_ok = c.get(FULLSTACK_UI_URL).status_code == 200
            return api_ok and ui_ok
    except Exception:
        return False


requires_fullstack = pytest.mark.skipif(
    not _stack_reachable(),
    reason=(
        f"Full stack not reachable at {FULLSTACK_API_URL} / {FULLSTACK_UI_URL} — "
        "run: docker compose -f docker-compose.test.yml --profile fullstack up -d --build"
    ),
)


@pytest_asyncio.fixture
async def fs_client():
    """Authenticated client against deployed api-test service."""
    async with MyClawClient(FULLSTACK_API_URL) as client:
        try:
            await client.health()
        except Exception as exc:
            pytest.skip(f"API not reachable: {exc}")
        await client.login(
            os.environ.get("INIT_ADMIN_EMAIL", "admin@test.myclaw.local"),
            os.environ.get("INIT_ADMIN_PASSWORD", "testpassword123"),
        )
        yield client
