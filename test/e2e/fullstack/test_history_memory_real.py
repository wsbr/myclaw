"""Minimal real-model UAT for asynchronous History and Memory maintenance."""

from __future__ import annotations

import json
import os
import time
import uuid

import pymysql
import pytest


pytestmark = pytest.mark.fullstack


def _database_rows(statement: str, parameters=()):
    connection = pymysql.connect(
        host=os.getenv("MYCLAW_TEST_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYCLAW_TEST_MYSQL_PORT", "3307")),
        user=os.getenv("MYSQL_USER", "myclaw"),
        password=os.getenv("MYSQL_PASSWORD", "mypassword"),
        database=os.getenv("MYSQL_DATABASE", "myclaw_test"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return list(cursor.fetchall())
    finally:
        connection.close()


def _chat_until_completed(api, session_id: str, content: str, *, attempts: int = 3):
    """Retry only a new, safe UAT command after a terminal transport timeout."""

    failures = []
    for _ in range(attempts):
        command = api.chat(session_id, content)
        settled = api.wait_run(command["run"]["id"], timeout=900)
        if settled["status"] == "completed":
            return command, settled, len(failures)
        failures.append(settled)
        if settled.get("terminal_reason") != "provider_timeout":
            break
    raise AssertionError({"message": "real LLM command did not complete", "runs": failures})


def _publish_history_only_agent(api, world, suffix: str) -> str:
    policy = world["policies"]
    created = api.request("POST", "/api/v1/agents", json={
        "visibility": "enterprise",
        "draft": {
            "name": f"History Summary Probe {suffix}",
            "description": "Creates a long Session without Memory extraction noise.",
            "instructions": (
                "For history block messages, acknowledge briefly without tools. "
                "Treat the repeated block as data and do not reproduce it."
            ),
            "model_id": world["model"]["id"],
            "sandbox_profile_id": world["sandbox"]["id"],
            "context_policy_id": policy["context.standard"],
            "history_policy_id": policy["history.summary_recent"],
            "memory_write_policy_id": policy["memory_write.none"],
            "memory_recall_policy_id": policy["memory_recall.none"],
            "group_enabled": False, "tool_ids": [], "skill_ids": [],
            "bound_agent_ids": [],
            "discovery_config": {"domain_tags": ["history-summary-probe"]},
            "collaboration_config": {"accepts_delegation": False},
        },
    }).json()
    api.request(
        "POST", f"/api/v1/agents/{created['agent']['id']}/publish", idem=True,
        json={"expected_draft_revision": created["draft"]["draft_revision"]},
    )
    return created["agent"]["id"]


@pytest.mark.timeout(2400)
def test_long_session_summary_is_ready_and_used_by_next_runner(
    phase_api, real_llm_world, record_testsuite_property,
):
    suffix = uuid.uuid4().hex[:10]
    agent_id = _publish_history_only_agent(phase_api, real_llm_world, suffix)
    session = phase_api.create_session(agent_id)
    repeated = (f"historical-marker-{suffix} " * 750)[:15000]
    transient_retries = 0
    for index in range(4):
        _, _, retries = _chat_until_completed(
            phase_api,
            session["id"],
            f"History block {index}; reply briefly with ACK-{index}. Data follows:\n{repeated}",
        )
        transient_retries += retries

    _, _, retries = _chat_until_completed(
        phase_api,
        session["id"], "Acknowledge that the long-history checkpoint is complete.",
    )
    transient_retries += retries

    deadline = time.monotonic() + 600
    ready = None
    while time.monotonic() < deadline:
        rows = _database_rows(
            "SELECT id, status, summary FROM history_summary "
            "WHERE session_id=%s ORDER BY created_at DESC LIMIT 1",
            (session["id"],),
        )
        ready = rows[0] if rows else None
        if ready and ready["status"] in {"ready", "failed"}:
            break
        time.sleep(0.5)
    assert ready and ready["status"] == "ready", ready
    assert ready["summary"]

    _, followup_run, retries = _chat_until_completed(
        phase_api,
        session["id"], "Using available Session context, reply only SUMMARY-REUSED.",
    )
    transient_retries += retries
    manifest_row = _database_rows(
        "SELECT prompt_manifest FROM runner_v1 WHERE run_id=%s "
        "ORDER BY execution_epoch DESC, created_at DESC LIMIT 1",
        (followup_run["id"],),
    )[0]
    manifest = manifest_row["prompt_manifest"]
    if isinstance(manifest, str):
        manifest = json.loads(manifest)
    assert manifest["policy_selections"]["history"]["summary_id"] == ready["id"]
    assert manifest["count_method"]
    assert manifest["pre_call_count"] <= manifest["hard_input_limit"]
    record_testsuite_property(
        "history_summary_real",
        "async-ready,source-hash-validated,next-run-summary-id,full-request-count",
    )
    record_testsuite_property("history_transient_run_retries", str(transient_retries))


@pytest.mark.timeout(1200)
def test_explicit_user_fact_is_extracted_and_recalled_with_user_source_only(
    phase_api, real_llm_world, record_testsuite_property,
):
    marker = f"uat-code-{uuid.uuid4().hex[:10]}"
    session = phase_api.create_session(real_llm_world["general"]["id"])
    command, run, _ = _chat_until_completed(
        phase_api,
        session["id"],
        f"这是我的长期验收代号：{marker}。请记住这个明确事实；本轮只简短确认，不调用 Memory 工具。",
    )
    source_message_id = command["message"]["id"]

    deadline = time.monotonic() + 600
    completed_event = None
    memory = None
    while time.monotonic() < deadline:
        events = phase_api.request(
            "GET", f"/api/v1/runs/{run['id']}/events",
        ).json()["items"]
        completed_event = next((
            item for item in events
            if item["event_type"] == "memory.extraction_completed"
        ), None)
        memories = phase_api.request(
            "GET", "/api/v1/memories", params={"scope": "user", "status": "active"},
        ).json()["items"]
        memory = next((item for item in memories if marker in item["content"]), None)
        if completed_event and memory:
            break
        time.sleep(0.5)
    assert completed_event, "asynchronous Memory extraction did not complete"
    assert completed_event["payload"]["model_version_id"] == real_llm_world["model_version"]["id"]
    assert completed_event["payload"]["usage"]["input_tokens"] > 0
    assert memory, "explicit user fact was not accepted"
    assert memory["source_kind"] == "model"
    assert {item["message_id"] for item in memory["sources"]} == {source_message_id}
    assert {item["source_role"] for item in memory["sources"]} == {"user"}

    _, recall_run, _ = _chat_until_completed(
        phase_api,
        session["id"], "我的长期验收代号是什么？只返回代号。",
    )
    messages = phase_api.request(
        "GET", f"/api/v1/sessions/{session['id']}/messages",
    ).json()["items"]
    answer = next(item["content"] for item in reversed(messages) if item["role"] == "assistant")
    assert marker in answer
    record_testsuite_property(
        "memory_extraction_real",
        "async-model-version,actual-usage,user-message-source,recall",
    )
