"""Verify full Docker stack is healthy before running feature tests."""

import os

import httpx
import pytest

from e2e.fullstack.conftest import FULLSTACK_API_URL, FULLSTACK_UI_URL, requires_fullstack

pytestmark = [pytest.mark.fullstack, requires_fullstack]


@pytest.mark.asyncio
async def test_api_health():
    async with httpx.AsyncClient() as c:
        resp = await c.get(f"{FULLSTACK_API_URL}/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_frontend_serves_app():
    async with httpx.AsyncClient() as c:
        resp = await c.get(FULLSTACK_UI_URL)
        assert resp.status_code == 200
        assert "html" in resp.text.lower()


@pytest.mark.asyncio
async def test_mock_llm_health():
    mock_url = os.environ.get("MYCLAW_MOCK_LLM_URL", "http://localhost:9090")
    async with httpx.AsyncClient() as c:
        resp = await c.get(f"{mock_url}/health")
        assert resp.status_code == 200
