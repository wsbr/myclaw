"""Integration tests for authentication."""

import pytest

from integration.conftest import requires_services

pytestmark = [pytest.mark.integration, requires_services]


@pytest.mark.asyncio
async def test_login_success(app_client):
    import os
    resp = await app_client.post(
        "/api/v1/auth/login",
        json={
            "email": os.environ["INIT_ADMIN_EMAIL"],
            "password": os.environ["INIT_ADMIN_PASSWORD"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"]
    assert data["user"]["role"] == "admin"


@pytest.mark.asyncio
async def test_login_invalid_credentials(app_client):
    resp = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "admin@test.myclaw.local", "password": "wrong-password"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_auth(app_client, auth_headers):
    resp = await app_client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["email"]

    unauth = await app_client.get("/api/v1/auth/me")
    assert unauth.status_code == 403
