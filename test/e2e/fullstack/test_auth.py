"""Full-stack: authentication against deployed API."""

import pytest

from e2e.fullstack.conftest import requires_fullstack

pytestmark = [pytest.mark.fullstack, requires_fullstack]


@pytest.mark.asyncio
async def test_login_and_me(fs_client):
    me = await fs_client.me()
    assert me["email"] == "admin@test.myclaw.local"
    assert me["role"] == "admin"


@pytest.mark.asyncio
async def test_invalid_login_rejected():
    import os
    import httpx
    from e2e.fullstack.conftest import FULLSTACK_API_URL

    async with httpx.AsyncClient(base_url=FULLSTACK_API_URL) as c:
        resp = await c.post("/api/v1/auth/login", json={
            "email": "admin@test.myclaw.local",
            "password": "wrong-password",
        })
        assert resp.status_code == 401
