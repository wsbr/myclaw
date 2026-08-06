"""Phase-one Agent Network and Agent-native Group vertical UAT."""

from __future__ import annotations

import json
import os
import time
import uuid

import pymysql
import pytest

from conftest import PhaseOneAPI


pytestmark = pytest.mark.fullstack


def _database_evidence(group_id: str, run_id: str) -> dict:
    connection = pymysql.connect(
        host=os.getenv("MYCLAW_GOAL1_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYCLAW_GOAL1_MYSQL_PORT", "3307")),
        user=os.getenv("MYCLAW_GOAL1_MYSQL_USER", "myclaw"),
        password=os.getenv("MYCLAW_GOAL1_MYSQL_PASSWORD", "mypassword"),
        database=os.getenv("MYCLAW_GOAL1_MYSQL_DATABASE", "myclaw_test"),
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            result = {}
            statements = {
                "formation_count": (
                    "SELECT COUNT(*) AS value FROM group_formation_proposal WHERE group_session_id=%s",
                    group_id,
                ),
                "members": (
                    "SELECT gm.agent_id,grm.agent_version_id,grm.participation_status,"
                    "grm.membership_policy,grm.capability_boundary,grm.budget_snapshot "
                    "FROM group_membership gm JOIN group_run gr ON gr.group_session_id=gm.group_session_id "
                    "JOIN group_run_member grm ON grm.group_run_id=gr.id AND grm.membership_id=gm.id "
                    "WHERE gm.group_session_id=%s AND gr.run_id=%s ORDER BY gm.agent_id",
                    (group_id, run_id),
                ),
                "turn_agents": (
                    "SELECT DISTINCT r.agent_id FROM agent_turn t JOIN runner_v1 r ON r.id=t.runner_id "
                    "WHERE t.run_id=%s AND t.status='completed' ORDER BY r.agent_id",
                    run_id,
                ),
                "tool_invocations": (
                    "SELECT tool_key,status,result_data FROM tool_invocation WHERE run_id=%s ORDER BY created_at",
                    run_id,
                ),
                "agent_invocations": (
                    "SELECT caller_agent_id,target_agent_id,status,error_code FROM agent_invocation "
                    "WHERE run_id=%s ORDER BY created_at",
                    run_id,
                ),
                "events": (
                    "SELECT event_type,payload FROM execution_event WHERE run_id=%s ORDER BY sequence",
                    run_id,
                ),
            }
            for name, (statement, parameters) in statements.items():
                cursor.execute(statement, parameters)
                rows = list(cursor.fetchall())
                result[name] = rows[0]["value"] if name == "formation_count" else rows
            return result
    finally:
        connection.close()


def _approve_tool(api: PhaseOneAPI, action: dict) -> None:
    api.request(
        "POST", f"/api/v1/tool-confirmations/{action['id']}/approve", idem=True,
        headers={"If-Match": f'W/"{action["version"]}"'},
        json={"arguments_hash": action["arguments_hash"], "reason": "Group UAT execution"},
    )


def _wait_group_run(api: PhaseOneAPI, session_id: str, run_id: str, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    handled: set[str] = set()
    last = None
    while time.monotonic() < deadline:
        last = api.request("GET", f"/api/v1/runs/{run_id}").json()
        if last["lifecycle"] in {"completed", "cancelled"}:
            return last
        actions = api.request(
            "GET", f"/api/v1/sessions/{session_id}/required-actions",
            params={"status": "pending"},
        ).json()["items"]
        for action in actions:
            if action["id"] in handled or action.get("run_id") != run_id:
                continue
            handled.add(action["id"])
            if action["type"] == "tool_confirmation":
                _approve_tool(api, action)
            elif action["type"] in {"tool", "session_grant"}:
                api.request(
                    "POST", f"/api/v1/approvals/{action['id']}/approve", idem=True,
                    headers={"If-Match": f'W/"{action["version"]}"'},
                    json={
                        "subject_hash": action["subject_hash"],
                        "reason": "Group UAT execution",
                    },
                )
            elif action["type"] == "reporter_selection":
                raise AssertionError(f"Reporter unexpectedly unavailable: {action}")
        time.sleep(0.2)
    raise AssertionError(f"GroupRun did not settle: {last}")


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


@pytest.mark.timeout(900)
def test_agent_network_group_vertical_uat(
    phase_api: PhaseOneAPI, assembled_world: dict, record_testsuite_property,
):
    api, world = phase_api, assembled_world
    product = world["product_manager"]
    development = world["developer"]
    test = world["test_engineer"]
    external = world["external_supplier"]
    selected = [product["id"], development["id"], test["id"]]

    # The directory is a projection of the exact current published versions.
    directory = api.request(
        "GET", "/api/v1/agent-network/cards",
        params={"group_enabled": "true", "limit": 100},
    ).json()["items"]
    by_id = {item["agent_id"]: item for item in directory}
    assert set(selected) <= by_id.keys()
    for member in (product, development, test):
        assert by_id[member["id"]]["agent_version_id"] == member["published"]["id"]
        assert by_id[member["id"]]["availability"] == "available"
        resolved = api.request("GET", f"/api/v1/agent-network/agents/{member['id']}").json()
        assert resolved["card_content_hash"] == by_id[member["id"]]["card_content_hash"]

    session = api.request(
        "POST", "/api/v1/sessions", idem=True,
        json={"entry_agent_id": product["id"]},
    ).json()

    # API + database creation gate: ten consecutive direct selections succeed.
    creations = []
    for attempt in range(10):
        goal = (
            "CAPACITY-RISK-NETWORK-V2 Build a real capacity risk calculator with executable "
            f"boundary tests and an Artifact. EXTERNAL-AGENT:{external['id']}"
            if attempt == 0 else f"GROUP-CREATE-ONLY-{attempt}: verify direct three-member creation"
        )
        group = api.request(
            "POST", f"/api/v1/sessions/{session['id']}/groups", idem=True,
            json={
                "member_agent_ids": selected,
                "reporter_agent_id": product["id"],
                "goal": goal,
                "shared_context_refs": [],
            },
        ).json()
        assert group["membership_policy"] == "fixed_for_run"
        assert group["initiator_agent_id"] == product["id"]
        runs = api.request("GET", f"/api/v1/groups/{group['id']}/runs").json()["items"]
        assert len(runs) == 1 and len(runs[0]["members"]) == 3
        creations.append((group, runs[0]))
    assert len({group["id"] for group, _run in creations}) == 10
    record_testsuite_property("group_creation_10_of_10", "passed")

    capacity_group, capacity = creations[0]
    result = _wait_group_run(api, session["id"], capacity["run"]["id"])
    assert result["outcome"] == "succeeded", result

    # Drain the nine simple groups as an HTTP/runtime recovery sanity gate.
    for _group, group_run in creations[1:]:
        settled = _wait_group_run(api, session["id"], group_run["run"]["id"])
        assert settled["outcome"] == "succeeded"

    messages = api.request(
        "GET", f"/api/v1/groups/{capacity_group['id']}/messages",
    ).json()["items"]
    real_agent_senders = {item["sender_agent_id"] for item in messages if item["sender_agent_id"]}
    assert set(selected) <= real_agent_senders
    assert any("DELIVERY-READY" in item["content"] for item in messages)
    assert any("TEST-VERIFIED" in item["content"] for item in messages)

    graph = api.request(
        "GET", f"/api/v1/group-runs/{capacity['id']}/interaction-graph",
    ).json()
    edges = {
        (item["source_agent_id"], item["target_agent_id"], item["cause_type"])
        for item in graph["edges"]
    }
    assert (product["id"], development["id"], "message") in edges
    assert (development["id"], test["id"], "message") in edges
    assert (test["id"], development["id"], "message") in edges
    assert (development["id"], product["id"], "message") in edges
    assert (product["id"], test["id"], "agent_invocation") in edges

    evidence = _database_evidence(capacity_group["id"], capacity["run"]["id"])
    assert evidence["formation_count"] == 0
    assert len(evidence["members"]) == 3
    assert {item["agent_version_id"] for item in evidence["members"]} == {
        product["published"]["id"], development["published"]["id"], test["published"]["id"],
    }
    assert all(item["membership_policy"] == "fixed_for_run" for item in evidence["members"])
    assert set(selected) <= {item["agent_id"] for item in evidence["turn_agents"]}
    assert any(item["status"] == "succeeded" for item in evidence["agent_invocations"])
    rejected_invites = [
        item for item in evidence["events"]
        if item["event_type"] == "group.membership_invitation_rejected"
    ]
    assert len(rejected_invites) == 1
    assert _json(rejected_invites[0]["payload"])["reason_code"] == "membership_policy_fixed_for_run"
    tool_rows = evidence["tool_invocations"]
    executions = [item for item in tool_rows if item["tool_key"] == "execute"]
    assert len(executions) == 2 and all(item["status"] == "succeeded" for item in executions)
    assert all("CAPACITY-SELF-TESTS-PASSED" in json.dumps(_json(item["result_data"])) for item in executions)

    artifacts = api.request(
        "GET", f"/api/v1/sessions/{session['id']}/artifacts",
        params={"status": "published", "limit": 100},
    ).json()["items"]
    artifact = next(item for item in artifacts if item["name"] == "capacity_risk_calculator.py")
    downloaded = api.request("GET", f"/api/v1/artifacts/{artifact['id']}/download").text
    assert "def capacity_risk" in downloaded
    assert "class CapacityRiskTests" in downloaded

    # Search the same durable Group history on two independent GroupRuns.
    history_runs = []
    for attempt in (1, 2):
        created = api.request(
            "POST", f"/api/v1/groups/{capacity_group['id']}/runs", idem=True,
            json={
                "content": f"CAPACITY-RISK-NETWORK-V2 HISTORY-CHECK {attempt}",
                "recipient_agent_id": product["id"],
                "reporter_agent_id": product["id"],
                "reference_run_ids": [capacity["id"]],
            },
        ).json()
        assert _wait_group_run(api, session["id"], created["run"]["id"])["outcome"] == "succeeded"
        history_runs.append(created)
    for created in history_runs:
        events = api.request("GET", f"/api/v1/runs/{created['run']['id']}/events").json()["items"]
        assert len([
            event for event in events
            if event["event_type"] == "tool.succeeded"
            and event.get("payload", {}).get("tool_key") == "search_group_history"
        ]) == 1

    final_group_run = api.request(
        "GET", f"/api/v1/group-runs/{capacity['id']}",
    ).json()
    assert final_group_run["summary_epoch"] == final_group_run["delivered_summary_epoch"] == 1
    assert "Capacity risk calculator delivered" in final_group_run["run"]["result_summary"]
    record_testsuite_property("agent_network_group_vertical", "passed")
