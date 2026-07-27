"""Integration tests for Plan mode API endpoints."""

import pytest

from integration.conftest import requires_services

pytestmark = [pytest.mark.integration, requires_services]


GOAL = "Analyze Q3 sales performance and summarize key trends"


@pytest.mark.asyncio
async def test_plan_generate_returns_draft(app_client, auth_headers, plan_agent):
    session_resp = await app_client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        json={"agent_id": plan_agent["id"], "mode": "plan"},
    )
    assert session_resp.status_code == 201
    session_id = session_resp.json()["id"]

    plan_resp = await app_client.post(
        f"/api/v1/sessions/{session_id}/plan/generate",
        headers=auth_headers,
        json={"goal": GOAL},
    )
    assert plan_resp.status_code == 200, plan_resp.text
    plan = plan_resp.json()

    assert plan["status"] == "draft"
    assert plan["goal"]
    assert len(plan["steps"]) >= 1
    assert all(s["status"] == "pending" for s in plan["steps"])


@pytest.mark.asyncio
async def test_plan_update_steps(app_client, auth_headers, plan_agent):
    session_id = (await app_client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        json={"agent_id": plan_agent["id"], "mode": "plan"},
    )).json()["id"]

    plan = (await app_client.post(
        f"/api/v1/sessions/{session_id}/plan/generate",
        headers=auth_headers,
        json={"goal": GOAL},
    )).json()

    updated_steps = [
        {**plan["steps"][0], "description": "Updated step description"},
        *plan["steps"][1:],
    ]
    update_resp = await app_client.put(
        f"/api/v1/sessions/{session_id}/plan/{plan['plan_id']}",
        headers=auth_headers,
        json={"steps": updated_steps},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["steps"][0]["description"] == "Updated step description"


@pytest.mark.asyncio
async def test_plan_execute_creates_task(app_client, auth_headers, plan_agent, celery_eager):
    session_id = (await app_client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        json={"agent_id": plan_agent["id"], "mode": "plan"},
    )).json()["id"]

    plan = (await app_client.post(
        f"/api/v1/sessions/{session_id}/plan/generate",
        headers=auth_headers,
        json={"goal": GOAL},
    )).json()

    exec_resp = await app_client.post(
        f"/api/v1/sessions/{session_id}/plan/{plan['plan_id']}/execute",
        headers=auth_headers,
    )
    assert exec_resp.status_code == 200, exec_resp.text
    body = exec_resp.json()
    assert body["task_id"]
    assert body["plan_id"] == plan["plan_id"]

    task_resp = await app_client.get(
        f"/api/v1/tasks/{body['task_id']}",
        headers=auth_headers,
    )
    assert task_resp.status_code == 200
    task = task_resp.json()
    assert task["status"] in ("completed", "running", "failed")
