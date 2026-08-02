from __future__ import annotations

import subprocess
import threading
import time
import uuid
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from conftest import PhaseOneAPI


pytestmark = pytest.mark.fullstack


def _session(api: PhaseOneAPI, agent_id: str, title: str) -> dict:
    return api.request("POST", "/api/v1/sessions", idem=True, json={
        "entry_agent_id": agent_id, "origin_type": "web", "title": title,
    }).json()


def _chat(api: PhaseOneAPI, session_id: str, content: str) -> dict:
    return api.request(
        "POST", f"/api/v1/sessions/{session_id}/messages", idem=True,
        json={"content": content, "mode": "chat"},
    ).json()


def _events(api: PhaseOneAPI, run_id: str) -> list[dict]:
    return api.request("GET", f"/api/v1/runs/{run_id}/events").json()["items"]


def _approve(api: PhaseOneAPI, action: dict, reason: str) -> None:
    api.request(
        "POST", f"/api/v1/tool-confirmations/{action['id']}/approve", idem=True,
        headers={"If-Match": f'W/"{action["version"]}"'},
        json={"arguments_hash": action["arguments_hash"], "reason": reason},
    )


def _capture_sse(api: PhaseOneAPI, session_id: str, after_sequence: int, ready: threading.Event) -> list[dict]:
    frames: list[dict] = []
    timeout = httpx.Timeout(connect=10, read=api.run_timeout, write=10, pool=10)
    with httpx.Client(base_url=api.base_url, headers=api.auth, timeout=timeout, trust_env=False) as client:
        with client.stream(
            "GET", f"/api/v1/sessions/{session_id}/events/stream",
            params={"after_sequence": after_sequence},
            headers={"Last-Event-ID": str(after_sequence)},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            ready.set()
            event_type = ""
            data = ""
            event_id = ""
            for line in response.iter_lines():
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data += line[6:]
                elif line.startswith("id: "):
                    event_id = line[4:]
                elif line == "" and event_type:
                    payload = json.loads(data) if data else {}
                    frames.append({"event": event_type, "id": event_id, "data": payload})
                    if event_type == "run.completed":
                        return frames
                    event_type = data = event_id = ""
    return frames


def _collector_logs() -> str:
    test_root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        ["docker", "compose", "-f", str(test_root / "docker-compose.test.yml"),
         "logs", "--no-color", "otel-collector-test"],
        cwd=test_root, check=True, timeout=30, capture_output=True, text=True,
    ).stdout


def _restart(*services: str):
    test_root = Path(__file__).resolve().parents[2]
    subprocess.run(
        ["docker", "compose", "-f", str(test_root / "docker-compose.test.yml"), "restart", *services],
        cwd=test_root, check=True, timeout=60,
    )


def _publish_next(api: PhaseOneAPI, agent_id: str, label: str) -> str:
    draft = api.request("GET", f"/api/v1/agents/{agent_id}/draft").json()
    updated = api.request(
        "PATCH", f"/api/v1/agents/{agent_id}/draft",
        headers={"If-Match": f'W/"{draft["draft_revision"]}"'},
        json={"instructions": f"{draft['instructions']}\nPublished acceptance revision: {label}."},
    ).json()
    return api.request(
        "POST", f"/api/v1/agents/{agent_id}/publish", idem=True,
        json={"expected_draft_revision": updated["draft_revision"]},
    ).json()["published_version"]["id"]


@pytest.mark.timeout(1200)
def test_phase_one_acceptance(
    phase_api: PhaseOneAPI, assembled_world: dict, record_testsuite_property,
):
    api, world = phase_api, assembled_world
    record_property = record_testsuite_property
    record_property("commit_sha", os.getenv("MYCLAW_ACCEPTANCE_COMMIT_SHA", "uncommitted"))
    record_property("model", os.getenv("MYCLAW_REAL_LLM_MODEL", "mock"))
    finance = world["finance"]["id"]
    operations = world["operations"]["id"]
    orders = world["orders"]["id"]
    outsider = world["outsider"]["id"]

    # 1. Builder assembly is fully published and inspectable as an immutable version.
    detail = api.request("GET", f"/api/v1/agents/{finance}").json()
    assert detail["agent"]["status"] == "published"
    assert detail["draft"]["model_id"] == world["model"]["id"]
    assert detail["draft"]["sandbox_profile_id"] == world["sandbox"]["id"]
    assert detail["draft"]["skill_ids"] == [world["skill"]["id"]]
    assert set(detail["draft"]["bound_agent_ids"]) == {operations, orders, outsider}
    version = api.request(
        "GET", f"/api/v1/agents/{finance}/versions/{world['finance']['published']['id']}",
    ).json()
    assert version["publication_status"] == "published" and version["content_hash"]

    # The published entry Agent is callable through its scoped Agent API Key.
    key_record = api.request(
        "POST", f"/api/v1/agents/{finance}/api-keys", idem=True,
        json={"name": "phase-one-acceptance", "caller_label": "docker-e2e"},
    ).json()
    assert key_record["secret"] and key_record["prefix"]
    api_key_secret = key_record["secret"]
    try:
        with httpx.Client(
            base_url=api.base_url, timeout=30, trust_env=False,
            headers={"Authorization": f"Bearer {api_key_secret}"},
        ) as agent_client:
            api_session_response = agent_client.post(
                "/api/v1/sessions", headers={"Idempotency-Key": str(uuid.uuid4())},
                json={"entry_agent_id": finance, "origin_type": "api", "user_id": api.user_id,
                      "title": "Agent API acceptance"},
            )
            assert api_session_response.status_code == 201, api_session_response.text
            api_session = api_session_response.json()
            assert api_session["origin_type"] == "api" and api_session["entry_agent_id"] == finance
            api_chat_response = agent_client.post(
                f"/api/v1/sessions/{api_session['id']}/messages",
                headers={"Idempotency-Key": str(uuid.uuid4())},
                json={"content": "Reply briefly to confirm the Agent API entry point; do not call tools.",
                      "mode": "chat"},
            )
            assert api_chat_response.status_code == 202, api_chat_response.text
            assert api.wait_run(api_chat_response.json()["run"]["id"])["outcome"] == "succeeded"
    finally:
        api.request("DELETE", f"/api/v1/agents/{finance}/api-keys/{key_record['id']}", idem=True)
    record_property("scene_1_builder_assembly_publish", "passed")

    # 2. Chat survives worker/Web disconnects, waits for confirmation, and executes a durable file Tool.
    chat_session = _session(api, finance, "Chat acceptance")
    chat = _chat(
        api, chat_session["id"],
        "Run sandbox command and sandbox file for acceptance. You must call execute with argv "
        "[\"python\",\"-c\",\"from pathlib import Path; p=Path('acceptance.txt'); "
        "p.write_text('sandbox-e2e'); print(p.read_text())\"] and must not simulate the result.",
    )
    action = api.wait_action(chat_session["id"], "tool_confirmation")
    _restart("runtime-worker-test", "sandbox-worker-test")
    assert action["tool_key"] == "execute"
    _approve(api, action, "Acceptance approval")
    # A fresh HTTP client simulates reconnect after the initiating connection is gone.
    with httpx.Client(base_url=api.base_url, timeout=30, headers=api.auth, trust_env=False) as reconnected:
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            settled = reconnected.get(f"/api/v1/runs/{chat['run']['id']}").json()
            if settled["lifecycle"] == "completed":
                break
            time.sleep(0.25)
        else:
            raise AssertionError(f"Reconnect polling did not settle Run: {settled}")
    assert settled["outcome"] == "succeeded"
    timeline = _events(api, chat["run"]["id"])
    assert {event["event_type"] for event in timeline} >= {
        "tool.confirmation_required", "tool_confirmation.approved", "tool.succeeded", "run.completed",
    }
    assert any("sandbox-e2e" in (item.get("content") or "") for item in api.request(
        "GET", f"/api/v1/sessions/{chat_session['id']}/messages",
    ).json()["items"])

    # Streaming model deltas and reliable SSE events share one reconnectable endpoint.
    high_watermark = api.request("GET", f"/api/v1/sessions/{chat_session['id']}/events").json()[
        "session_high_watermark"
    ]
    ready = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        stream_future = executor.submit(_capture_sse, api, chat_session["id"], high_watermark, ready)
        assert ready.wait(10), "SSE connection did not become ready"
        time.sleep(0.5)
        streamed_chat = _chat(
            api, chat_session["id"],
            "Reply with a short streaming acknowledgement and do not call any tool.",
        )
        assert api.wait_run(streamed_chat["run"]["id"])["outcome"] == "succeeded"
        frames = stream_future.result(timeout=api.run_timeout + 10)
    assert any(
        frame["event"] == "model.delta"
        and frame["data"].get("run_id") == streamed_chat["run"]["id"]
        and frame["data"].get("delta")
        for frame in frames
    )
    assert any(
        frame["event"] == "run.completed"
        and frame["data"].get("run_id") == streamed_chat["run"]["id"]
        for frame in frames
    )

    # A file becomes downloadable only after the model explicitly writes and publishes it.
    artifact_chat = _chat(
        api, chat_session["id"],
        "Publish acceptance artifact: first call write_file to create acceptance-artifact.txt "
        "with exact content artifact-e2e, then call publish_artifact for that path as text/plain. "
        "Do not use execute and do not stop before both Tool calls succeed.",
    )
    write_confirmation = api.wait_action(chat_session["id"], "tool_confirmation")
    assert write_confirmation["tool_key"] == "write_file"
    _approve(api, write_confirmation, "Allow deterministic acceptance file")
    publish_confirmation = api.wait_action(chat_session["id"], "tool_confirmation", timeout=api.run_timeout)
    assert publish_confirmation["tool_key"] == "publish_artifact"
    _approve(api, publish_confirmation, "Publish deterministic acceptance Artifact")
    assert api.wait_run(artifact_chat["run"]["id"])["outcome"] == "succeeded"
    artifacts = api.request(
        "GET", f"/api/v1/sessions/{chat_session['id']}/artifacts", params={"status": "published"},
    ).json()["items"]
    artifact = next(item for item in artifacts if item["name"] == "acceptance-artifact.txt")
    download = api.request("GET", f"/api/v1/artifacts/{artifact['id']}/download")
    assert download.content == b"artifact-e2e"
    record_property("scene_2_chat_sse_tools_recovery_artifact", "passed")

    # 6. Tool governance applies safely to queued invocations and to new calls immediately.
    governed = _chat(
        api, chat_session["id"],
        "Run sandbox command while governance changes. You must call execute with argv "
        "[\"/bin/echo\",\"sandbox-e2e\"] and must not simulate the result.",
    )
    pending = api.wait_action(chat_session["id"], "tool_confirmation")
    assert pending["tool_key"] == "execute"
    execute_tool = api.request("GET", f"/api/v1/platform-tools/{world['tools']['execute']['id']}").json()
    disabled = api.request(
        "PATCH", f"/api/v1/platform-tools/{execute_tool['id']}", idem=True,
        headers={"If-Match": f'W/"{execute_tool["config_version"]}"'}, json={"enabled": False},
    ).json()
    try:
        _approve(api, pending, "Verify disabled preflight")
        governed_result = api.wait_run(governed["run"]["id"])
        governed_events = {event["event_type"] for event in _events(api, governed["run"]["id"])}
        assert governed_result["outcome"] == "failed"
        assert not {"tool.dispatched", "tool.succeeded"}.intersection(governed_events)
        api.request(
            "PATCH", f"/api/v1/platform-tools/{execute_tool['id']}", idem=True,
            headers={"If-Match": f'W/"{disabled["config_version"]}"'},
            json={"enabled": True, "confirmation_policy": "never"},
        )
        immediate = _chat(
            api, chat_session["id"],
            "Run sandbox command after immediate policy update. You must call execute with argv "
            "[\"/bin/echo\",\"sandbox-e2e\"] and must not simulate the result.",
        )
        assert api.wait_run(immediate["run"]["id"])["outcome"] == "succeeded"
        assert not api.request(
            "GET", f"/api/v1/sessions/{chat_session['id']}/required-actions",
            params={"status": "pending", "type": "tool_confirmation"},
        ).json()["items"]
    finally:
        current_execute = api.request("GET", f"/api/v1/platform-tools/{execute_tool['id']}").json()
        if not current_execute["enabled"] or current_execute["confirmation_policy"] != "always":
            api.request(
                "PATCH", f"/api/v1/platform-tools/{execute_tool['id']}", idem=True,
                headers={"If-Match": f'W/"{current_execute["config_version"]}"'},
                json={"enabled": True, "confirmation_policy": "always"},
            )
    record_property("scene_6_tool_governance", "passed")

    # 3. A three-node Plan is edited, frozen, and runs its independent nodes in parallel.
    plan_session = _session(api, finance, "Plan acceptance")
    created_plan = api.request(
        "POST", f"/api/v1/sessions/{plan_session['id']}/plans", idem=True,
        json={"goal": "Parallel quarterly verification with direct text responses and no Tool calls"},
    ).json()["plan"]
    proposal = api.wait_resource(
        f"/api/v1/plans/{created_plan['id']}",
        lambda value: value["definition_status"] == "awaiting_confirmation",
    )
    nodes = [
        {"node_key": "finance", "title": "Finance", "goal": "Verify finance; return text directly and do not call any Tool", "acceptance_criteria": "Direct finance text evidence without Tool calls", "assigned_agent_id": finance},
        {"node_key": "operations", "title": "Operations", "goal": "Verify operations; return text directly and do not call any Tool", "acceptance_criteria": "Direct operations text evidence without Tool calls", "assigned_agent_id": operations},
        {"node_key": "orders", "title": "Orders", "goal": "Verify orders; return text directly and do not call any Tool", "acceptance_criteria": "Direct orders text evidence without Tool calls", "assigned_agent_id": orders},
    ]
    edited_plan = api.request(
        "PATCH", f"/api/v1/plans/{proposal['id']}", idem=True,
        headers={"If-Match": f'W/"{proposal["version"]}"'},
        json={"goal": proposal["goal"], "deliverable": "Combined evidence", "nodes": nodes,
              "dependencies": [], "expected_graph_revision": proposal["graph_revision"]},
    ).json()
    frozen_plan = api.request(
        "POST", f"/api/v1/plans/{proposal['id']}/confirm", idem=True,
        headers={"If-Match": f'W/"{edited_plan["version"]}"'},
        json={"expected_graph_revision": edited_plan["graph_revision"]},
    ).json()
    assert frozen_plan["definition_status"] == "frozen"
    assert api.wait_run(frozen_plan["run_id"])["outcome"] == "succeeded"
    plan_versions = {item["agent_id"]: item["agent_version_id"] for item in frozen_plan["declared_agents"]}
    plan_work = api.request("GET", f"/api/v1/plans/{proposal['id']}/work-items").json()["items"]
    assert len(plan_work) == 3 and {item["status"] for item in plan_work} == {"succeeded"}
    record_property("scene_3_plan_freeze_parallel_settlement", "passed")

    # 4. Three-Agent Group: edit/freeze, directed clarification, peer invocation, history search, outside rejection.
    group_session = _session(api, finance, "Group acceptance")
    formation = api.request(
        "POST", f"/api/v1/sessions/{group_session['id']}/group-formations", idem=True,
        json={"goal": "Finance, operations, and orders collaborate with direct text and no initial Tool calls"},
    ).json()
    formation = api.wait_resource(
        f"/api/v1/group-formations/{formation['id']}",
        lambda value: value["status"] == "awaiting_confirmation",
    )
    participants = [
        {"participant_key": "finance", "agent_id": finance, "is_entry": True, "initial_responsibility": "Own delivery"},
        {"participant_key": "operations", "agent_id": operations, "is_entry": False, "initial_responsibility": "Verify operations"},
        {"participant_key": "orders", "agent_id": orders, "is_entry": False, "initial_responsibility": "Verify orders"},
    ]
    work_items = [
        {"work_item_key": key, "participant_key": key, "title": key.title(),
         "goal": f"Verify {key}; return text directly and do not call any Tool",
         "acceptance_criteria": "Return direct text evidence without Tool calls"}
        for key in ("finance", "operations", "orders")
    ]
    edited_formation = api.request(
        "PATCH", f"/api/v1/group-formations/{formation['id']}", idem=True,
        headers={"If-Match": f'W/"{formation["version"]}"'},
        json={"goal": formation["current_revision"]["goal"], "bootstrap_context": "Shared confirmed context",
              "participants": participants, "initial_work_items": work_items,
              "suggested_edges": [
                  {"source_participant_key": "finance", "target_participant_key": "operations", "label": "Check operations", "rationale": "Peer verification"},
                  {"source_participant_key": "finance", "target_participant_key": "orders", "label": "Check orders", "rationale": "Peer verification"},
              ], "expected_revision_no": formation["current_revision_no"]},
    ).json()
    confirmed = api.request(
        "POST", f"/api/v1/group-formations/{formation['id']}/confirm", idem=True,
        headers={"If-Match": f'W/"{edited_formation["version"]}"'},
        json={"expected_revision_no": edited_formation["current_revision_no"]},
    ).json()
    group_id = confirmed["group_session"]["id"]
    initial_group_run = confirmed["group_run"]["id"]
    api.wait_run(confirmed["run"]["id"])
    frozen_group_versions = {
        item["group_node_id"]: item["agent_version_id"]
        for item in api.request("GET", f"/api/v1/group-runs/{initial_group_run}").json()["agent_versions"]
    }
    group_nodes = api.request("GET", f"/api/v1/groups/{group_id}/nodes").json()["items"]
    by_agent_node = {item["agent_id"]: item["id"] for item in group_nodes}

    clarification_run = api.request(
        "POST", f"/api/v1/groups/{group_id}/runs", idem=True,
        json={"content": "You must call request_clarification once for this Group and ask which reporting period to use.",
              "recipient_node_id": by_agent_node[operations], "reference_run_ids": []},
    ).json()
    clarification = api.wait_action(group_session["id"], "clarification")
    assert clarification["group_node_id"] == by_agent_node[operations]
    api.request(
        "POST", f"/api/v1/clarifications/{clarification['id']}/answer", idem=True,
        headers={"If-Match": f'W/"{clarification["version"]}"'}, json={"answer": "Use Q2"},
    )
    api.wait_run(clarification_run["run"]["id"])

    peer_run = api.request(
        "POST", f"/api/v1/groups/{group_id}/runs", idem=True,
        json={"content": f"You must call invoke_agent once for agent {orders} to provide secondary verification.",
              "recipient_node_id": by_agent_node[finance], "reference_run_ids": []},
    ).json()
    api.wait_run(peer_run["run"]["id"])
    assert "agent_invocation.succeeded" in {event["event_type"] for event in _events(api, peer_run["run"]["id"])}
    peer_runners = api.request("GET", f"/api/v1/runs/{peer_run['run']['id']}/runners").json()["items"]
    peer_traces = [api.request("GET", f"/api/v1/runners/{runner['id']}/trace").json() for runner in peer_runners]
    assert any(
        link["link_type"] == "agent_invocation" and link["linked_span_id"]
        for trace in peer_traces for link in trace["links"]
    )

    search_run = api.request(
        "POST", f"/api/v1/groups/{group_id}/runs", idem=True,
        json={"content": "You must call search_group_history once with query group and report the result.",
              "recipient_node_id": by_agent_node[finance], "reference_run_ids": []},
    ).json()
    api.wait_run(search_run["run"]["id"])
    assert any(
        event["event_type"] == "tool.succeeded" and event.get("payload", {}).get("tool_key") == "search_group_history"
        for event in _events(api, search_run["run"]["id"])
    )

    outside_run = api.request(
        "POST", f"/api/v1/groups/{group_id}/runs", idem=True,
        json={"content": f"You must call invoke_agent once for agent {outsider}; the Runtime must reject this graph-external target.",
              "recipient_node_id": by_agent_node[finance], "reference_run_ids": []},
    ).json()
    api.wait_run(outside_run["run"]["id"])
    assert "agent_invocation.rejected" in {event["event_type"] for event in _events(api, outside_run["run"]["id"])}
    record_property("scene_4_group_collaboration", "passed")

    # 5. Publishing new versions changes new Chat only; frozen Plan and Group mappings remain unchanged.
    new_finance_version = _publish_next(api, finance, "v2")
    new_operations_version = _publish_next(api, operations, "v2")
    assert {
        item["agent_id"]: item["agent_version_id"]
        for item in api.request("GET", f"/api/v1/plans/{proposal['id']}").json()["declared_agents"]
    } == plan_versions
    assert {
        item["group_node_id"]: item["agent_version_id"]
        for item in api.request("GET", f"/api/v1/group-runs/{initial_group_run}").json()["agent_versions"]
    } == frozen_group_versions
    fresh_chat = _chat(api, chat_session["id"], "Use the newly published finance version")
    api.wait_run(fresh_chat["run"]["id"])
    work = api.request("GET", f"/api/v1/runs/{fresh_chat['run']['id']}/work-items").json()["items"]
    assert work[0]["agent_version_id"] == new_finance_version
    assert new_operations_version != plan_versions[operations]
    record_property("scene_5_version_freezing", "passed")

    # 7. Restart while a Runner is active: recover the same WorkItem and keep a reliable terminal event.
    restart_chat = _chat(api, chat_session["id"], "slow runner restart acceptance")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        runners = api.request("GET", f"/api/v1/runs/{restart_chat['run']['id']}/runners").json()["items"]
        if runners and runners[0]["status"] == "running":
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"Runner never entered running state before restart: {runners}")
    _restart("runtime-worker-test")
    recovered = api.wait_run(restart_chat["run"]["id"], timeout=90)
    assert recovered["outcome"] == "succeeded"
    work_items_after = api.request("GET", f"/api/v1/runs/{restart_chat['run']['id']}/work-items").json()["items"]
    assert len(work_items_after) == 1
    runners_after = api.request("GET", f"/api/v1/runs/{restart_chat['run']['id']}/runners").json()["items"]
    assert len(runners_after) == 2
    assert {runner["status"] for runner in runners_after} == {"lost", "succeeded"}
    assert {runner["execution_epoch"] for runner in runners_after} == {1, 2}
    terminal_events = [event for event in _events(api, restart_chat["run"]["id"]) if event["event_type"] == "run.completed"]
    assert len(terminal_events) == 1

    # The actual OTLP exporter delivered both Runtime spans and an OTel Link to
    # the Collector; the database trace above remains the user-safe projection.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        otel_logs = _collector_logs()
        if "model_generation" in otel_logs and "invoke_agent.link" in otel_logs:
            break
        time.sleep(1)
    else:
        raise AssertionError("OTel Collector did not receive Runtime generation and AgentInvocation link spans")
    record_property("scene_7_worker_recovery_and_otel", "passed")


def test_required_contract_surface(phase_api: PhaseOneAPI):
    schema = phase_api.client.get("/openapi.json").json()["paths"]
    required = {
        "/api/v1/accounts/me", "/api/v1/accounts/me/change-user-id",
        "/api/v1/admin/audit-events", "/api/v1/accounts/me/audit-events",
        "/api/v1/builder/audit-events", "/api/v1/runs/{run_id}/events",
        "/api/v1/auth/enterprise/start", "/api/v1/auth/enterprise/callback",
        "/api/v1/agents/{agent_id}/versions/{version_id}",
    }
    assert required <= schema.keys()


def test_signed_cursor_and_error_envelope(phase_api: PhaseOneAPI, assembled_world):
    first = phase_api.request("GET", "/api/v1/agents", params={"limit": 1}).json()
    assert len(first["items"]) == 1 and first["next_cursor"]
    second = phase_api.request(
        "GET", "/api/v1/agents", params={"limit": 1, "cursor": first["next_cursor"]},
    ).json()
    assert second["items"] and second["items"][0]["id"] != first["items"][0]["id"]

    tampered = first["next_cursor"][:-1] + ("A" if first["next_cursor"][-1] != "A" else "B")
    response = phase_api.client.get(
        "/api/v1/agents", params={"limit": 1, "cursor": tampered}, headers=phase_api.auth,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]

    rebound = phase_api.client.get(
        "/api/v1/agents",
        params={"limit": 1, "cursor": first["next_cursor"], "status": "published"},
        headers=phase_api.auth,
    )
    assert rebound.status_code == 422
    assert rebound.json()["error"]["code"] == "invalid_cursor"

    missing = phase_api.client.get(
        "/api/v1/runs/00000000-0000-0000-0000-000000000000", headers=phase_api.auth,
    )
    assert missing.status_code == 404
    assert set(missing.json()["error"]) == {
        "code", "message", "request_id", "retryable", "details",
    }
