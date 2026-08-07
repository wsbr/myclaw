"""Phase-one Chat-only vertical acceptance against the deployed stack."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
import threading

import httpx
import pymysql
import pytest

from conftest import PhaseOneAPI


pytestmark = pytest.mark.fullstack


def _session(api: PhaseOneAPI, agent_id: str) -> dict:
    return api.request(
        "POST", "/api/v1/sessions", idem=True,
        json={"entry_agent_id": agent_id},
    ).json()


def _chat(api: PhaseOneAPI, session_id: str, content: str) -> dict:
    return api.request(
        "POST", f"/api/v1/sessions/{session_id}/messages", idem=True,
        json={"content": content},
    ).json()


def _approve(api: PhaseOneAPI, action: dict, reason: str = "Chat UAT approval") -> dict:
    return api.request(
        "POST", f"/api/v1/approvals/{action['id']}/approve", idem=True,
        headers={"If-Match": f'"{action["version"]}"'},
        json={"subject_hash": action["subject_hash"], "reason": reason},
    ).json()


def _compose(*args: str) -> None:
    test_root = Path(__file__).resolve().parents[2]
    compose_file = Path(os.getenv(
        "MYCLAW_COMPOSE_FILE", str(test_root / "docker-compose.test.yml"),
    ))
    aliases = {
        "runtime-worker-test": os.getenv("MYCLAW_RUNTIME_WORKER_SERVICE", "runtime-worker-test"),
        "sandbox-worker-test": os.getenv("MYCLAW_SANDBOX_WORKER_SERVICE", "sandbox-worker-test"),
    }
    command = ["docker", "compose"]
    compose_env = os.getenv("MYCLAW_COMPOSE_ENV_FILE")
    if compose_env:
        command.extend(["--env-file", compose_env])
    project = os.getenv("MYCLAW_COMPOSE_PROJECT") or os.getenv("COMPOSE_PROJECT_NAME")
    if project:
        command.extend(["-p", project])
    command.extend(["-f", str(compose_file), *(aliases.get(arg, arg) for arg in args)])
    subprocess.run(command, cwd=test_root, check=True, timeout=120)


def _database_counts(run_id: str) -> dict[str, int]:
    connection = pymysql.connect(
        host=os.getenv("MYCLAW_GOAL1_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYCLAW_GOAL1_MYSQL_PORT", "3307")),
        user=os.getenv("MYCLAW_GOAL1_MYSQL_USER", "myclaw"),
        password=os.getenv("MYCLAW_GOAL1_MYSQL_PASSWORD", "mypassword"),
        database=os.getenv("MYCLAW_GOAL1_MYSQL_DATABASE", "myclaw_test"),
    )
    try:
        with connection.cursor() as cursor:
            result = {}
            for key, query in {
                "work_items": "SELECT COUNT(*) FROM work_item WHERE run_id=%s",
                "runners": "SELECT COUNT(*) FROM runner_v1 WHERE run_id=%s",
                "tools": "SELECT COUNT(*) FROM tool_invocation WHERE run_id=%s",
                "execute_tools": "SELECT COUNT(*) FROM tool_invocation WHERE run_id=%s AND tool_key='execute'",
                "agent_invocations": "SELECT COUNT(*) FROM agent_invocation WHERE run_id=%s",
                "artifacts": "SELECT COUNT(*) FROM artifact WHERE work_item_id IN (SELECT id FROM work_item WHERE run_id=%s)",
            }.items():
                cursor.execute(query, (run_id,))
                result[key] = int(cursor.fetchone()[0])
            return result
    finally:
        connection.close()


def _raw_wait_run(client: httpx.Client, run_id: str, headers: dict[str, str]) -> dict:
    deadline = time.monotonic() + 90
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}", headers=headers)
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in {"completed", "cancelled", "failed"}:
            return last
        time.sleep(0.2)
    raise AssertionError(f"raw API Run did not settle: {last}")


def _capture_sse(
    api: PhaseOneAPI, session_id: str, after_sequence: int,
    ready: threading.Event, expected_run: list[str],
) -> list[dict]:
    frames: list[dict] = []
    headers = {**api.auth, "Last-Event-ID": str(after_sequence)}
    with httpx.Client(base_url=api.base_url, timeout=90, trust_env=False) as client:
        with client.stream(
            "GET", f"/api/v1/sessions/{session_id}/events/stream", headers=headers,
        ) as response:
            assert response.status_code == 200, response.text
            ready.set()
            frame: dict[str, str] = {}
            for line in response.iter_lines():
                if line == "":
                    if frame:
                        data = json.loads(frame.get("data") or "{}")
                        item = {
                            "id": int(frame["id"]) if frame.get("id") else None,
                            "event": frame.get("event", "message"), "data": data,
                        }
                        frames.append(item)
                        if (
                            item["event"] == "run.completed"
                            and expected_run and data.get("run_id") == expected_run[0]
                        ):
                            return frames
                    frame = {}
                    continue
                if line.startswith(":"):
                    continue
                key, _, value = line.partition(":")
                frame[key] = value.lstrip()
    return frames


@pytest.mark.timeout(2400)
def test_chat_clarification_working_plan_artifact_restart_and_ten_of_ten(
    phase_api: PhaseOneAPI, assembled_world: dict, record_testsuite_property,
):
    api = phase_api
    agent_id = assembled_world["audit_lead"]["id"]
    record_testsuite_property("scope", "phase-one-chat-only")
    write_file = next(
        item for item in api.request("GET", "/api/v1/platform-tools").json()["items"]
        if item["tool_key"] == "write_file"
    )
    if write_file["confirmation_policy"] != "session_grant":
        api.request(
            "PATCH", f"/api/v1/platform-tools/{write_file['id']}", idem=True,
            headers={"If-Match": f'W/"{write_file["config_version"]}"'},
            json={"enabled": True, "confirmation_policy": "session_grant"},
        )

    # A simple Chat does not acquire ceremonial planning state.
    simple_session = _session(api, agent_id)
    simple = _chat(api, simple_session["id"], "请用一句话确认收到任务，本次只需直接回复。")
    assert api.wait_run(simple["run"]["id"])["status"] == "completed"
    assert api.request(
        "GET", f"/api/v1/runs/{simple['run']['id']}/working-plans",
    ).json()["items"] == []

    # A complex Chat may use update_plan, but it still owns exactly one WorkItem.
    complex_run = _chat(
        api, simple_session["id"],
        "CHAT-EXACT-PLAN-V2：这是复杂任务。已确认事实：第二季度支付侧退款 100 笔、订单侧 "
        "97 笔，差异 3 笔；请基于这些现有事实完成分析，只调用 update_plan 一次并提交恰好"
        "三个已完成步骤，然后给出简短结论，不需要额外数据。",
    )["run"]
    assert api.wait_run(complex_run["id"])["status"] == "completed"
    plans = api.request("GET", f"/api/v1/runs/{complex_run['id']}/working-plans").json()["items"]
    assert len(plans) == 1
    assert plans[0]["revision"] == 1 and plans[0]["status"] == "closed"
    assert len(plans[0]["items"]) == 3
    assert _database_counts(complex_run["id"])["work_items"] == 1

    # Clarification answers resume the same WorkItem through a new Runner epoch.
    clarification_run = _chat(
        api, simple_session["id"],
        "CHAT-CLARIFY-ONLY-V2：请先确认统计口径，本轮应该采用哪个周期，然后再给出结论。",
    )["run"]
    clarification = api.wait_action(simple_session["id"], "clarification")
    work_item_id = clarification["work_item_id"]
    _compose("restart", "runtime-worker-test")
    answered = api.request(
        "POST", f"/api/v1/clarifications/{clarification['id']}/answer", idem=True,
        headers={"If-Match": f'"{clarification["version"]}"'},
        json={"answer": "采用 2026 年第二季度口径。"},
    ).json()
    assert answered["status"] == "answered"
    assert api.wait_run(clarification_run["id"])["status"] == "completed"
    work_items = api.request(
        "GET", f"/api/v1/runs/{clarification_run['id']}/work-items",
    ).json()["items"]
    assert [item["id"] for item in work_items] == [work_item_id]
    assert _database_counts(clarification_run["id"])["runners"] == 2

    # Stop the Sandbox while approval is pending, then approve and restart the
    # Runtime before bringing Sandbox back. Durable state must produce one write
    # and one immutable Artifact, never replay either side effect.
    artifact_run = _chat(
        api, simple_session["id"],
        "请把结论整理成可下载的《验收报告 2026.txt》，正文只写 artifact-e2e，"
        "完成文件创建和正式发布后再回复。",
    )["run"]
    approval = api.wait_action(simple_session["id"], "session_grant")
    assert approval["tool_key"] == "write_file"
    _compose("stop", "sandbox-worker-test")
    try:
        _approve(api, approval)
        waiting = api.wait_resource(
            f"/api/v1/runs/{artifact_run['id']}",
            lambda item: item["status"] == "waiting_tool",
        )
        assert waiting["status"] == "waiting_tool"
        _compose("restart", "runtime-worker-test")
    finally:
        _compose("start", "sandbox-worker-test")
    assert api.wait_run(artifact_run["id"], timeout=120)["status"] == "completed"
    artifacts = api.request(
        "GET", f"/api/v1/sessions/{simple_session['id']}/artifacts",
        params={"status": "published"},
    ).json()["items"]
    matches = [item for item in artifacts if item["work_item_id"] and item["name"] == "验收报告 2026.txt"]
    assert len(matches) == 1
    artifact = matches[0]
    download = api.request("GET", f"/api/v1/artifacts/{artifact['id']}/download")
    assert download.content == b"artifact-e2e"
    assert hashlib.sha256(download.content).hexdigest() == artifact["checksum"]
    counts = _database_counts(artifact_run["id"])
    assert counts["work_items"] == 1
    assert counts["tools"] == 2
    assert counts["execute_tools"] == 0
    assert counts["agent_invocations"] == 0
    assert counts["artifacts"] == 1

    # Count the first complete action-checklist flow above, then repeat it nine
    # times in isolated Sessions so every sample must clarify, resume, obtain a
    # fresh session grant, write, publish, download, and avoid execute/delegation.
    statuses = ["completed"]
    for index in range(2, 11):
        repeated_session = _session(api, agent_id)
        clarification_run = _chat(
            api, repeated_session["id"],
            f"第 {index} 次行动清单：缺失测试负责人、验收时间和优先级；"
            "CHAT-CLARIFY-ONLY-V2：请先确认统计口径，本轮应该采用哪个周期，然后再给出结论。",
        )["run"]
        clarification = api.wait_action(repeated_session["id"], "clarification")
        api.request(
            "POST", f"/api/v1/clarifications/{clarification['id']}/answer", idem=True,
            headers={"If-Match": f'"{clarification["version"]}"'},
            json={"answer": "测试负责人为 QA，验收时间为本周五，优先级 P1，采用 2026 年第二季度口径。"},
        )
        assert api.wait_run(clarification_run["id"], timeout=180)["status"] == "completed"
        repeated_run = _chat(
            api, repeated_session["id"],
            "请把结论整理成可下载的《验收报告 2026.txt》，正文只写 artifact-e2e，"
            "完成文件创建和正式发布后再回复。",
        )["run"]
        approval = api.wait_action(repeated_session["id"], "session_grant")
        assert approval["tool_key"] == "write_file"
        _approve(api, approval, reason=f"Chat UAT full-flow sample {index}")
        statuses.append(api.wait_run(repeated_run["id"], timeout=180)["status"])
        artifacts = api.request(
            "GET", f"/api/v1/sessions/{repeated_session['id']}/artifacts",
            params={"status": "published"},
        ).json()["items"]
        matches = [item for item in artifacts if item["name"] == "验收报告 2026.txt"]
        assert len(matches) == 1
        content = api.request(
            "GET", f"/api/v1/artifacts/{matches[0]['id']}/download",
        ).content
        assert content == b"artifact-e2e"
        assert hashlib.sha256(content).hexdigest() == matches[0]["checksum"]
        counts = _database_counts(repeated_run["id"])
        assert counts["tools"] == 2 and counts["artifacts"] == 1
        assert counts["execute_tools"] == counts["agent_invocations"] == 0
    assert statuses == ["completed"] * 10
    record_testsuite_property("chat_uat", "10/10")


@pytest.mark.timeout(300)
def test_agent_api_key_raw_user_id_header_only(
    phase_api: PhaseOneAPI, assembled_world: dict,
):
    api = phase_api
    agent_id = assembled_world["audit_lead"]["id"]
    key = api.request(
        "POST", f"/api/v1/agents/{agent_id}/api-keys", idem=True,
        json={"name": "chat-raw-user-uat", "caller_label": "chat-vertical"},
    ).json()
    raw_user_id = f"raw-uat-{uuid.uuid4().hex[:12]}"
    auth = {"Authorization": f"Bearer {key['secret']}"}
    raw = {**auth, "X-MyClaw-User-Id": raw_user_id}
    try:
        with httpx.Client(base_url=api.base_url, timeout=30, trust_env=False) as client:
            missing = client.post(
                "/api/v1/sessions", headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
                json={"entry_agent_id": agent_id},
            )
            assert missing.status_code == 400

            provisioned = client.post(
                "/api/v1/identity/preprovision",
                headers={**raw, "Idempotency-Key": str(uuid.uuid4())},
            )
            assert provisioned.status_code == 201, provisioned.text
            assert provisioned.json()["user_id"] == raw_user_id

            forbidden_body_identity = client.post(
                "/api/v1/sessions",
                headers={**raw, "Idempotency-Key": str(uuid.uuid4())},
                json={"entry_agent_id": agent_id, "user_id": raw_user_id},
            )
            assert forbidden_body_identity.status_code == 422

            session_response = client.post(
                "/api/v1/sessions",
                headers={**raw, "Idempotency-Key": str(uuid.uuid4())},
                json={"entry_agent_id": agent_id},
            )
            assert session_response.status_code == 201, session_response.text
            session = session_response.json()
            assert session["user_id"] == raw_user_id and session["origin_type"] == "api"

            command = client.post(
                f"/api/v1/sessions/{session['id']}/messages",
                headers={**raw, "Idempotency-Key": str(uuid.uuid4())},
                json={"content": "请直接确认已收到 API Chat 任务。"},
            )
            assert command.status_code == 202, command.text
            run = _raw_wait_run(client, command.json()["run"]["id"], raw)
            assert run["status"] == "completed"
    finally:
        api.request("DELETE", f"/api/v1/agents/{agent_id}/api-keys/{key['id']}", idem=True)


@pytest.mark.timeout(300)
def test_sse_last_event_id_and_rest_continuation(
    phase_api: PhaseOneAPI, assembled_world: dict,
):
    api = phase_api
    session = _session(api, assembled_world["audit_lead"]["id"])
    baseline = api.request(
        "GET", f"/api/v1/sessions/{session['id']}/events",
    ).json()["session_high_watermark"]
    ready = threading.Event()
    expected_run: list[str] = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        stream = executor.submit(
            _capture_sse, api, session["id"], baseline, ready, expected_run,
        )
        assert ready.wait(10)
        command = _chat(api, session["id"], "SSE 续读验收：请直接回复收到。")
        expected_run.append(command["run"]["id"])
        assert api.wait_run(command["run"]["id"])["status"] == "completed"
        frames = stream.result(timeout=30)
    persisted = [item for item in frames if item["id"] is not None]
    assert persisted and all(item["id"] > baseline for item in persisted)
    assert any(
        item["event"] == "run.completed"
        and item["data"].get("run_id") == command["run"]["id"]
        for item in frames
    )
    rest = api.request(
        "GET", f"/api/v1/runs/{command['run']['id']}/events",
        params={"after_sequence": baseline},
    ).json()
    assert rest["scanned_through_sequence"] >= max(item["id"] for item in persisted)
    assert any(item["event_type"] == "run.completed" for item in rest["items"])

    # A disconnected client resumes from its persisted maximum id and receives
    # only newer durable frames.
    resume_from = max(item["id"] for item in persisted)
    ready = threading.Event()
    expected_run = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        stream = executor.submit(
            _capture_sse, api, session["id"], resume_from, ready, expected_run,
        )
        assert ready.wait(10)
        command2 = _chat(api, session["id"], "SSE 断线恢复第二次验收：请直接回复收到。")
        expected_run.append(command2["run"]["id"])
        assert api.wait_run(command2["run"]["id"])["status"] == "completed"
        resumed = stream.result(timeout=30)
    assert all(
        item["id"] is None or item["id"] > resume_from
        for item in resumed
    )
