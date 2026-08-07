"""Phase-one Agent Network and Agent-native Group vertical UAT."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import time
import uuid

import pymysql
import pytest

from conftest import PhaseOneAPI
from app.services.verification_service import CAPACITY_ARTIFACT_NAMES, CAPACITY_CASES


pytestmark = pytest.mark.fullstack


CAPACITY_GROUP_CONTRACT = (
    "CAPACITY-RISK-NETWORK-V2 Build and deliver a real capacity risk calculator. "
    "Create requirements.md, capacity_risk.py, test_report.md, acceptance_report.md; "
    "execute T01-T09 and publish all four as Group Artifacts. "
    "The exact rule is utilization=committed/capacity*100; risk is low below 75, "
    "medium from 75 through 90 inclusive, and high above 90; over_capacity_hours is "
    "max(committed-capacity,0). capacity<=0, committed<0, and missing committed each "
    "exit 2. Emit compact JSON with sorted keys and a trailing newline; normalize integral "
    "capacity/committed/over-capacity values to JSON integers, while utilization_percent "
    "must be a JSON float rounded to two decimals. Errors use "
    "{\"error\":{\"code\":CODE,\"message\":MESSAGE}}. "
    "Every successful JSON object has exactly these keys: committed_hours, "
    "over_capacity_hours, risk, total_capacity_hours, utilization_percent. "
    "Run exactly these argv in order: "
    "T01 python3 capacity_risk.py --capacity 120 --committed 138; "
    "T02 python3 capacity_risk.py --capacity 100 --committed 74; "
    "T03 python3 capacity_risk.py --capacity 100 --committed 75; "
    "T04 python3 capacity_risk.py --capacity 100 --committed 90; "
    "T05 python3 capacity_risk.py --capacity 100 --committed 91; "
    "T06 python3 capacity_risk.py --capacity 100 --committed 0; "
    "T07 python3 capacity_risk.py --capacity 0 --committed 10; "
    "T08 python3 capacity_risk.py --capacity 100 --committed -1; "
    "T09 python3 capacity_risk.py --capacity 100 with no --committed. "
    "Error code/message pairs are invalid_capacity/total_capacity_hours must be greater "
    "than 0, invalid_committed/committed_hours must be greater than or equal to 0, and "
    "missing_committed/--committed is required. requirements.md must contain these literal "
    "contract lines: 'utilization_percent = committed_hours / total_capacity_hours * 100', "
    "'low: utilization < 75', 'medium: 75 <= utilization <= 90', "
    "'high: utilization > 90', 'over_capacity_hours = max(committed_hours - "
    "total_capacity_hours, 0)', 'total_capacity_hours > 0', and "
    "'committed_hours >= 0', plus labels T01 through T09. test_report.md must contain "
    "9/9, argv, stdout, stderr, exit code, and labels T01 through T09. acceptance_report.md "
    "must contain capacity_risk.v1, accepted, 9/9, and all four Artifact filenames."
)


def _compose(*args: str) -> None:
    test_root = Path(__file__).resolve().parents[2]
    compose_file = Path(os.getenv(
        "MYCLAW_COMPOSE_FILE", str(test_root / "docker-compose.test.yml"),
    ))
    command = ["docker", "compose"]
    compose_env = os.getenv("MYCLAW_COMPOSE_ENV_FILE")
    if compose_env:
        command.extend(["--env-file", compose_env])
    project = os.getenv("MYCLAW_COMPOSE_PROJECT") or os.getenv("COMPOSE_PROJECT_NAME")
    if project:
        command.extend(["-p", project])
    aliases = {
        "sandbox-worker-test": os.getenv("MYCLAW_SANDBOX_WORKER_SERVICE", "sandbox-worker-test"),
    }
    command.extend(["-f", str(compose_file), *(aliases.get(arg, arg) for arg in args)])
    subprocess.run(command, cwd=test_root, check=True, timeout=120)


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
                "tool_attempts": (
                    "SELECT ti.tool_key,ta.status,ta.request_snapshot,ta.response_snapshot,"
                    "ta.request_hash,ta.outcome_confirmed,ta.execution_epoch,ta.fencing_token,"
                    "ta.started_at,ta.ended_at FROM tool_attempt ta "
                    "JOIN tool_invocation ti ON ti.id=ta.tool_invocation_id "
                    "WHERE ti.run_id=%s ORDER BY ta.created_at,ta.id",
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
                result[name] = rows
            return result
    finally:
        connection.close()


def _approve_tool(api: PhaseOneAPI, action: dict) -> None:
    api.request(
        "POST", f"/api/v1/tool-confirmations/{action['id']}/approve", idem=True,
        headers={"If-Match": f'W/"{action["version"]}"'},
        json={"arguments_hash": action["arguments_hash"], "reason": "Group UAT execution"},
    )


def _approve_action(api: PhaseOneAPI, action: dict) -> None:
    if action["type"] == "tool_confirmation":
        _approve_tool(api, action)
        return
    if action["type"] not in {"tool", "session_grant"}:
        raise AssertionError(f"Unexpected Group action during Sandbox restart: {action}")
    api.request(
        "POST", f"/api/v1/approvals/{action['id']}/approve", idem=True,
        headers={"If-Match": f'W/"{action["version"]}"'},
        json={"subject_hash": action["subject_hash"], "reason": "Group UAT execution"},
    )


def _wait_run_action(
    api: PhaseOneAPI, session_id: str, run_id: str, timeout: float = 180,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        actions = api.request(
            "GET", f"/api/v1/sessions/{session_id}/required-actions",
            params={"status": "pending"},
        ).json()["items"]
        action = next((item for item in actions if item.get("run_id") == run_id), None)
        if action is not None:
            return action
        time.sleep(0.2)
    raise AssertionError(f"No pending Tool action for Group Run {run_id}")


def _wait_group_run(api: PhaseOneAPI, session_id: str, run_id: str, timeout: float = 600) -> dict:
    deadline = time.monotonic() + timeout
    handled: set[str] = set()
    last = None
    while time.monotonic() < deadline:
        last = api.request("GET", f"/api/v1/runs/{run_id}").json()
        if last["status"] in {"completed", "cancelled", "failed"}:
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


def _wait_verification(api: PhaseOneAPI, run_id: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = api.request(
            "GET", f"/api/v1/runs/{run_id}/verification-results",
        ).json()["items"]
        if items:
            return items[0]
        time.sleep(0.2)
    raise AssertionError("capacity_risk.v1 VerificationResult was not produced")


def _settle_capacity_with_recovery(
    api: PhaseOneAPI, *, session_id: str, group: dict, group_run: dict,
    product_agent_id: str, external_agent_id: str, max_retries: int = 2,
) -> tuple[dict, dict, dict, int, str]:
    """Retry only as a new GroupRun; every failed attempt remains immutable evidence."""
    current = group_run
    retries = 0
    first_verification_status = "missing"
    while True:
        settled = _wait_group_run(api, session_id, current["run"]["id"])
        verification = _wait_verification(api, current["run"]["id"])
        if retries == 0:
            first_verification_status = verification["status"]
        if settled["status"] == "completed" and verification["status"] == "passed":
            return current, settled, verification, retries, first_verification_status
        assert retries < max_retries, {
            "settled": settled,
            "verification": verification,
            "retries": retries,
        }
        retries += 1
        previous_group_run_id = current["id"]
        current = api.request(
            "POST", f"/api/v1/groups/{group['id']}/runs", idem=True,
            json={
                "content": (
                    f"{CAPACITY_GROUP_CONTRACT} EXTERNAL-AGENT:{external_agent_id} "
                    f"RECOVERY-RETRY-{retries}: prior GroupRun {previous_group_run_id} "
                    "is immutable failed evidence. Independently execute all T01-T09 and "
                    "publish four new Run-scoped Artifacts; prior claims and partial files "
                    "do not count as completion."
                ),
                "recipient_agent_id": product_agent_id,
                "reporter_agent_id": product_agent_id,
                "reference_run_ids": [previous_group_run_id],
            },
        ).json()


@pytest.mark.timeout(3600)
def test_agent_network_group_vertical_uat(
    phase_api: PhaseOneAPI, assembled_world: dict, record_testsuite_property,
):
    api, world = phase_api, assembled_world
    product = world["product_manager"]
    development = world["developer"]
    test = world["test_engineer"]
    external = world["project_manager"]
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
    settled_runs: dict[str, dict] = {}
    first_attempt_statuses: list[str] = []
    recovery_retries = 0
    for attempt in range(10):
        goal = (
            f"{CAPACITY_GROUP_CONTRACT} EXTERNAL-AGENT:{external['id']}"
            if attempt < 5 else f"GROUP-CREATE-ONLY-{attempt}: verify direct three-member creation"
        )
        if attempt == 0:
            _compose("stop", "sandbox-worker-test")
        try:
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
            run_id = runs[0]["run"]["id"]
            if attempt == 0:
                first_action = _wait_run_action(api, session["id"], run_id)
                _approve_action(api, first_action)
                api.wait_resource(
                    f"/api/v1/runs/{run_id}",
                    lambda item: item["status"] == "waiting_tool",
                )
        finally:
            if attempt == 0:
                _compose("start", "sandbox-worker-test")
        if attempt < 5:
            final_run, settled, _verification, retries, first_status = (
                _settle_capacity_with_recovery(
                    api, session_id=session["id"], group=group, group_run=runs[0],
                    product_agent_id=product["id"], external_agent_id=external["id"],
                )
            )
            creations[-1] = (group, final_run)
            settled_runs[final_run["run"]["id"]] = settled
            first_attempt_statuses.append(first_status)
            recovery_retries += retries
    assert len({group["id"] for group, _run in creations}) == 10
    record_testsuite_property("group_creation_10_of_10", "passed")
    record_testsuite_property(
        "group_full_flow_first_attempt_success",
        f"{sum(status == 'passed' for status in first_attempt_statuses)}/5",
    )
    record_testsuite_property("group_full_flow_final_success", "5/5")
    record_testsuite_property("group_full_flow_retry_count", str(recovery_retries))

    capacity_group, capacity = creations[0]
    result = settled_runs[capacity["run"]["id"]]
    assert result["status"] == "completed" and result["terminal_reason"] == "completed", result

    # Drain four more complete Group flows and the five creation-only flows.
    for _group, group_run in creations[1:]:
        settled = settled_runs.get(group_run["run"]["id"])
        if settled is None:
            settled = _wait_group_run(api, session["id"], group_run["run"]["id"])
        assert settled["status"] == "completed" and settled["terminal_reason"] == "completed"

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
    assert len(executions) == 9 and all(item["status"] == "succeeded" for item in executions)
    attempts = [item for item in evidence["tool_attempts"] if item["tool_key"] == "execute"]
    assert len(attempts) == 9
    for attempt, case in zip(attempts, CAPACITY_CASES, strict=True):
        request = _json(attempt["request_snapshot"])
        response = _json(attempt["response_snapshot"])["data"]
        assert request["argv"] == list(case.argv)
        assert attempt["request_hash"] == hashlib.sha256(json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        assert response["exit_code"] == case.exit_code
        assert response["stdout"] == case.stdout
        assert response["stderr"] == ""
        assert bool(attempt["outcome_confirmed"]) is True
        assert attempt["execution_epoch"] >= 1 and attempt["fencing_token"] >= 1
        assert attempt["started_at"] and attempt["ended_at"] >= attempt["started_at"]

    artifacts = api.request(
        "GET", f"/api/v1/sessions/{session['id']}/artifacts",
        params={"status": "published", "limit": 100},
    ).json()["items"]
    capacity_work_items = {
        item["id"] for item in api.request(
            "GET", f"/api/v1/runs/{capacity['run']['id']}/work-items",
        ).json()["items"]
    }
    capacity_artifacts = [
        item for item in artifacts
        if item["name"] in CAPACITY_ARTIFACT_NAMES
        and item["work_item_id"] in capacity_work_items
    ]
    assert {item["name"] for item in capacity_artifacts} == set(CAPACITY_ARTIFACT_NAMES)
    assert len(capacity_artifacts) == 4
    downloaded = {}
    for artifact in capacity_artifacts:
        content = api.request(
            "GET", f"/api/v1/artifacts/{artifact['id']}/download",
        ).content
        assert len(content) == artifact["size_bytes"]
        assert hashlib.sha256(content).hexdigest() == artifact["checksum"]
        downloaded[artifact["name"]] = content.decode()
    assert "utilization_percent" in downloaded["capacity_risk.py"]
    assert "T09" in downloaded["requirements.md"]
    assert "9/9" in downloaded["test_report.md"]
    assert "capacity_risk.v1" in downloaded["acceptance_report.md"]

    verification = _wait_verification(api, capacity["run"]["id"])
    assert verification["rule_key"] == "capacity_risk.v1"
    assert verification["status"] == "passed", verification
    assert len(verification["source_refs"]) == 13
    assert len([item for item in verification["observed_facts"] if "case_id" in item]) == 9
    assert len([item for item in verification["observed_facts"] if "artifact_id" in item]) == 4
    runners = api.request(
        "GET", f"/api/v1/runs/{capacity['run']['id']}/runners",
    ).json()["items"]
    traces = [
        api.request("GET", f"/api/v1/runners/{runner['id']}/trace").json()
        for runner in runners
    ]
    assert {item["tool_attempt_id"] for item in verification["observed_facts"] if "case_id" in item} <= {
        attempt_id
        for trace in traces
        for attempt_id in trace["ledger_refs"]["tool_attempt_ids"]
    }
    assert any(
        verification["id"] in trace["ledger_refs"]["verification_result_ids"]
        for trace in traces
    )
    record_testsuite_property("capacity_risk_verification", "9/9 + 4/4 passed")

    full_flows = 1
    for group, group_run in creations[1:5]:
        run_id = group_run["run"]["id"]
        group_messages = api.request(
            "GET", f"/api/v1/groups/{group['id']}/messages",
        ).json()["items"]
        assert set(selected) <= {
            item["sender_agent_id"] for item in group_messages if item["sender_agent_id"]
        }
        run_evidence = _database_evidence(group["id"], run_id)
        run_attempts = [
            item for item in run_evidence["tool_attempts"] if item["tool_key"] == "execute"
        ]
        assert len(run_attempts) == 9
        assert all(item["status"] == "succeeded" for item in run_attempts)
        run_work_items = {
            item["id"] for item in api.request(
                "GET", f"/api/v1/runs/{run_id}/work-items",
            ).json()["items"]
        }
        run_artifacts = [
            item for item in artifacts
            if item["name"] in CAPACITY_ARTIFACT_NAMES
            and item["work_item_id"] in run_work_items
        ]
        assert {item["name"] for item in run_artifacts} == set(CAPACITY_ARTIFACT_NAMES)
        for artifact in run_artifacts:
            content = api.request(
                "GET", f"/api/v1/artifacts/{artifact['id']}/download",
            ).content
            assert len(content) == artifact["size_bytes"]
            assert hashlib.sha256(content).hexdigest() == artifact["checksum"]
        assert _wait_verification(api, run_id)["status"] == "passed"
        full_flows += 1
    assert full_flows == 5
    record_testsuite_property("group_full_flow", "5/5")

    # Search the same durable Group history on two independent GroupRuns.
    history_runs = []
    for attempt in (1, 2):
        created = api.request(
            "POST", f"/api/v1/groups/{capacity_group['id']}/runs", idem=True,
            json={
                "content": (
                    f"CAPACITY-RISK-NETWORK-V2 HISTORY-CHECK {attempt} "
                    "HISTORY-SEARCH-REQUIRED：这是新的独立 GroupRun；无论之前的 Group 摘要或"
                    "引用 Run 是否已有结果，当前 Run 都必须独立调用 search_group_history 恰好"
                    "一次后再回复，之前 Run 的检索不满足本 Run。"
                ),
                "recipient_agent_id": product["id"],
                "reporter_agent_id": product["id"],
                "reference_run_ids": [capacity["id"]],
            },
        ).json()
        assert _wait_group_run(api, session["id"], created["run"]["id"])["status"] == "completed"
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
    assert any("Capacity risk calculator delivered" in item["content"] for item in messages)
    record_testsuite_property("agent_network_group_vertical", "passed")
