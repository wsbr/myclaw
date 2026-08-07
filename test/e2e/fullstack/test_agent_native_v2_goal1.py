"""Current-SHA Goal 1 smoke for the Agent-native v2 execution path."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

import pymysql
import pytest

from conftest import PhaseOneAPI


pytestmark = pytest.mark.fullstack


FINAL_MESSAGE = "Agent-native v2 full-stack smoke passed."


def _idem() -> str:
    return str(uuid.uuid4())


def _mysql_rows(run_id: str) -> dict:
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
            queries = {
                "runner": (
                    "SELECT id,status,lease_owner,execution_epoch,capability_snapshot_id "
                    "FROM runner_v1 WHERE run_id=%s ORDER BY created_at"
                ),
                "outbox": (
                    "SELECT id,topic,action,aggregate_id,status,attempt_count "
                    "FROM outbox_command WHERE aggregate_id IN "
                    "(SELECT id FROM runner_v1 WHERE run_id=%s) ORDER BY created_at"
                ),
                "turns": (
                    "SELECT id,runner_id,turn_no,status,stop_reason,`usage`,turn_items "
                    "FROM agent_turn WHERE run_id=%s ORDER BY turn_no"
                ),
                "attempts": (
                    "SELECT p.id,p.agent_turn_id,p.model_version_id,p.attempt_no,p.provider,"
                    "p.status,p.provider_request_id,p.received_payload,p.tool_call_observed,"
                    "p.stop_reason,p.`usage` FROM provider_attempt p JOIN agent_turn t "
                    "ON t.id=p.agent_turn_id WHERE t.run_id=%s ORDER BY t.turn_no,p.attempt_no"
                ),
                "invocations": (
                    "SELECT id,runner_id,tool_key,status,provider_tool_call_id,arguments,"
                    "result_data FROM tool_invocation WHERE run_id=%s ORDER BY created_at"
                ),
                "checkpoints": (
                    "SELECT id,runner_id,sequence_no,checkpoint_type,last_completed_step,"
                    "safe_to_resume FROM runner_checkpoint WHERE work_item_id IN "
                    "(SELECT id FROM work_item WHERE run_id=%s) ORDER BY sequence_no"
                ),
                "events": (
                    "SELECT event_type,actor_type,actor_id,entity_type,entity_id,sequence "
                    "FROM execution_event WHERE run_id=%s ORDER BY sequence"
                ),
                "messages": (
                    "SELECT id,role,sender_type,content,status FROM message "
                    "WHERE run_id=%s ORDER BY created_at,id"
                ),
            }
            result = {}
            for name, query in queries.items():
                cursor.execute(query, (run_id,))
                result[name] = list(cursor.fetchall())
            return result
    finally:
        connection.close()


def _json_value(value):
    return json.loads(value) if isinstance(value, str) else value


def _write_evidence(payload: dict) -> None:
    target = os.getenv("MYCLAW_GOAL1_SMOKE_EVIDENCE")
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def test_agent_native_v2_goal1_fullstack_smoke(
    phase_api: PhaseOneAPI, assembled_world: dict, record_property,
):
    api = phase_api
    sha = os.getenv("MYCLAW_ACCEPTANCE_COMMIT_SHA", "unbound")
    suffix = sha[:12] if sha != "unbound" else uuid.uuid4().hex[:12]
    record_property("commit_sha", sha)
    record_property("suite_version", "agent-native-v2.2")
    real_model = assembled_world["real_model"]
    record_property(
        "flow",
        "api-outbox-v2-worker-real-model-plain-final" if real_model
        else "api-outbox-v2-worker-mock-tool-result-plain-final",
    )

    if real_model:
        agent = assembled_world["personal"]
        version = assembled_world["model_version"]
        conformance = assembled_world["conformance"]
        session = api.request(
            "POST", "/api/v1/sessions", idem=True,
            json={"entry_agent_id": agent["id"]},
        ).json()
        command = api.request(
            "POST", f"/api/v1/sessions/{session['id']}/messages", idem=True,
            json={
                "content": (
                    "Agent-native v2 UAT smoke: use the shared coordinator and reply with one "
                    "short acknowledgement. Do not call a tool for this request."
                ),
            },
        ).json()
        run = api.wait_run(command["run"]["id"], timeout=180)
        assert run["status"] == "completed", run
        assert run["terminal_reason"] == "completed", run
        messages = api.request(
            "GET", f"/api/v1/sessions/{session['id']}/messages",
        ).json()["items"]
        assert messages[-1]["role"] == "assistant" and messages[-1]["content"].strip()
        database = _mysql_rows(run["id"])
        assert database["runner"] and all(
            item["status"] == "succeeded" for item in database["runner"]
        )
        assert database["turns"] and database["attempts"]
        assert all(item["status"] == "succeeded" for item in database["attempts"])
        assert all(
            _json_value(item["usage"]).get("input_tokens", 0) > 0
            for item in database["attempts"]
        )
        assert "run.completed" in [item["event_type"] for item in database["events"]]
        _write_evidence({
            "schema": "myclaw.agent_native_v2.goal1_smoke.v1",
            "commit_sha": sha,
            "suite_version": conformance["suite_version"],
            "model": {
                "logical_model_id": assembled_world["model"]["id"],
                "model_version_id": version["id"],
                "model_version_content_hash": version["content_hash"],
                "conformance_run_id": conformance["id"],
            },
            "agent": {
                "agent_id": agent["id"],
                "agent_version_id": agent["published"]["id"],
                "agent_version_content_hash": agent["published"]["content_hash"],
            },
            "execution": {
                "session_id": session["id"],
                "run_id": run["id"],
                "final_message": messages[-1]["content"],
                "database": database,
            },
        })
        return

    logical = api.request(
        "POST", "/api/v1/admin/models", idem=True,
        json={"key": f"goal1-v2-mock-{suffix}", "name": "Goal 1 v2 Mock"},
    ).json()
    version = api.request(
        "POST", f"/api/v1/admin/models/{logical['id']}/versions", idem=True,
        headers={"If-Match": f'W/"{logical["version"]}"'},
        json={
            "provider": "openai_compatible",
            "model_name": "mock-model",
            "endpoint": "http://mock-llm:9090/v1",
            "credential_ref": None,
            "default_parameters": {"temperature": 0, "max_tokens": 512},
            "capabilities": {
                "streaming": True,
                "native_tool_calling": True,
                "parallel_tool_calling": False,
                "tool_result_continuation": True,
                "usage_in_stream": True,
                "stop_reason": True,
                "context_window": 8192,
                "structured_output": "none",
            },
        },
    ).json()
    conformance = api.request(
        "POST",
        f"/api/v1/admin/models/{logical['id']}/versions/{version['id']}/conformance",
        idem=True,
    ).json()
    assert conformance["suite_version"] == "agent-native-v2.2"
    assert conformance["status"] == "passed", conformance
    coordination = next(
        probe for probe in conformance["result_summary"]["probes"]
        if probe["name"] == "runtime_coordination_tool_schemas"
    )
    assert coordination["passed"] is True
    assert [case["tool_name"] for case in coordination["details"]["cases"]] == [
        "update_plan", "ask_user", "invoke_agent",
    ]

    logical = api.request("GET", f"/api/v1/admin/models/{logical['id']}").json()
    activated = api.request(
        "POST",
        f"/api/v1/admin/models/{logical['id']}/versions/{version['id']}/activate",
        idem=True,
        headers={"If-Match": f'W/"{logical["version"]}"'},
    ).json()
    assert activated["current_version_id"] == version["id"]

    tools = api.request("GET", "/api/v1/platform-tools").json()["items"]
    write_file = next(item for item in tools if item["tool_key"] == "write_file")
    if not write_file["enabled"] or write_file["confirmation_policy"] != "never":
        write_file = api.request(
            "PATCH", f"/api/v1/platform-tools/{write_file['id']}", idem=True,
            headers={"If-Match": f'W/"{write_file["config_version"]}"'},
            json={"enabled": True, "confirmation_policy": "never"},
        ).json()
    policies = {
        item["policy_key"]: item["id"]
        for item in api.request("GET", "/api/v1/policy-definitions").json()["items"]
    }
    sandbox = next(
        item for item in api.request("GET", "/api/v1/sandbox-profiles").json()["items"]
        if item["profile_key"] == "local"
    )
    created = api.request("POST", "/api/v1/agents", json={
        "visibility": "private",
        "draft": {
            "name": f"Goal 1 v2 Smoke {suffix}",
            "description": "Current-SHA Agent-native v2 full-stack smoke",
            "instructions": (
                "Use write_file exactly once when requested, consume its Tool Result, "
                "then return an ordinary final Assistant Message."
            ),
            "model_id": logical["id"],
            "sandbox_profile_id": sandbox["id"],
            "context_policy_id": policies["context.standard"],
            "history_policy_id": policies["history.recent"],
            "memory_write_policy_id": policies["memory_write.none"],
            "memory_recall_policy_id": policies["memory_recall.none"],
            "group_enabled": False,
            "tool_ids": [write_file["id"]],
            "skill_ids": [],
            "bound_agent_ids": [],
        },
    }).json()
    agent_id = created["agent"]["id"]
    check = api.request(
        "GET", f"/api/v1/agents/{agent_id}/draft/capability-check",
    ).json()
    assert check["publishable"] is True, check
    published = api.request(
        "POST", f"/api/v1/agents/{agent_id}/publish", idem=True,
        json={"expected_draft_revision": created["draft"]["draft_revision"]},
    ).json()
    agent_version = published["published_version"]
    assert agent_version["model_version_id"] == version["id"]

    session = api.request(
        "POST", "/api/v1/sessions", idem=True,
        json={"entry_agent_id": agent_id},
    ).json()
    command = api.request(
        "POST", f"/api/v1/sessions/{session['id']}/messages", idem=True,
        json={
            "content": (
                "goal1-v2-fullstack-smoke: write the requested evidence file, "
                "consume the Tool Result, then finish with a normal message."
            ),
        },
    ).json()
    run = api.wait_run(command["run"]["id"], timeout=90)
    assert run["status"] == "completed", run
    assert run["terminal_reason"] == "completed", run

    messages = api.request(
        "GET", f"/api/v1/sessions/{session['id']}/messages",
    ).json()["items"]
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == FINAL_MESSAGE

    deadline = time.monotonic() + 10
    database = {}
    while time.monotonic() < deadline:
        database = _mysql_rows(run["id"])
        if database.get("attempts") and database.get("events"):
            break
        time.sleep(0.2)

    assert len(database["runner"]) == 2
    assert all(runner["status"] == "succeeded" for runner in database["runner"])
    assert all(runner["lease_owner"].startswith("runtime-v2:") for runner in database["runner"])
    assert all(runner["capability_snapshot_id"] for runner in database["runner"])
    runner_outbox = [item for item in database["outbox"] if item["topic"] == "runtime.runner"]
    assert len(runner_outbox) == 2
    assert all(item["status"] == "completed" for item in runner_outbox)
    assert all(item["attempt_count"] == 1 for item in runner_outbox)
    assert [turn["stop_reason"] for turn in database["turns"]] == [
        "tool_use", "end_turn",
    ]
    assert [turn["status"] for turn in database["turns"]] == [
        "waiting_actions", "completed",
    ]
    assert len(database["attempts"]) == 2
    assert all(attempt["status"] == "succeeded" for attempt in database["attempts"])
    assert all(attempt["received_payload"] for attempt in database["attempts"])
    assert [attempt["tool_call_observed"] for attempt in database["attempts"]] == [1, 0]
    assert all(_json_value(attempt["usage"])["input_tokens"] > 0 for attempt in database["attempts"])
    assert len(database["invocations"]) == 1
    invocation = database["invocations"][0]
    assert invocation["tool_key"] == "write_file"
    assert invocation["status"] == "succeeded"
    assert _json_value(invocation["result_data"])["data"]["path"] == "goal1-v2-smoke.txt"
    checkpoint_types = [item["checkpoint_type"] for item in database["checkpoints"]]
    assert "lease_acquired" in checkpoint_types
    assert "durable_wait" in checkpoint_types
    assert "turn_completed" in checkpoint_types
    assert "run.completed" in [item["event_type"] for item in database["events"]]
    assert database["messages"][-1]["content"] == FINAL_MESSAGE

    evidence = {
        "schema": "myclaw.agent_native_v2.goal1_smoke.v1",
        "commit_sha": sha,
        "suite_version": conformance["suite_version"],
        "model": {
            "logical_model_id": logical["id"],
            "model_version_id": version["id"],
            "model_version_content_hash": version["content_hash"],
            "conformance_run_id": conformance["id"],
            "coordination_schema_bundle_hash": coordination["details"]["schema_bundle_hash"],
        },
        "agent": {
            "agent_id": agent_id,
            "agent_version_id": agent_version["id"],
            "agent_version_content_hash": agent_version["content_hash"],
        },
        "execution": {
            "session_id": session["id"],
            "run_id": run["id"],
            "final_message": FINAL_MESSAGE,
            "database": database,
        },
    }
    _write_evidence(evidence)
