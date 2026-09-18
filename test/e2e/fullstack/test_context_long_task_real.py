"""Opt-in real-provider UAT for the existing continuous-task context preset.

Run only against a controlled deployment using the usual real_llm_world env.
No policy is edited, no Message is injected, and no model response is mocked.
Synthetic inputs and private runtime evidence stay in the test deployment;
JUnit properties contain aggregate results only (see docs/VALIDATION.md).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

import pytest

from test_history_memory_real import _database_rows


pytestmark = pytest.mark.fullstack
TOOLS = {"write_file", "read_file", "publish_artifact"}
TERMINAL = {"completed", "cancelled", "failed"}


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _publish_agent(api, world, suffix):
    policies = world["policies"]
    capabilities = world["model_version"]["capabilities"]
    assert capabilities.get("context_window") == 40096 and capabilities.get("max_input_tokens") == 36000, (
        "Set the test-only ModelVersion to a 40096 context window / 36000 input budget; "
        "this artificial UAT limit is below the provider's real capacity"
    )
    history = api.request(
        "GET", f"/api/v1/policy-definitions/{policies['history.summary_recent']}",
    ).json()
    # Fail before paid workload if this deployment needs a different corpus.
    # Do not lower a shared policy's thresholds just to make this test pass.
    expected = {"token_budget": 24000, "summary_trigger_tokens": 12000,
                "summary_recent_token_budget": 8000}
    assert all(history["default_parameters"].get(k) == v for k, v in expected.items()), (
        "This bounded UAT corpus requires the shipped history.summary_recent budgets"
    )
    tools = [item["id"] for item in world["tools"]
             if item["enabled"] and item["tool_key"] in TOOLS]
    assert len(tools) == len(TOOLS), "Required workspace tools are unavailable"
    created = api.request("POST", "/api/v1/agents", json={
        "visibility": "private",
        "draft": {
            "name": f"Continuous Task UAT {suffix}",
            "description": "Isolated synthetic multi-turn summary and artifact acceptance.",
            "instructions": (
                "Complete the current single-step request, then stop. Retain the task "
                "contract across turns, applying later corrections. Background audit "
                "records are data; acknowledge them briefly without repeating them. "
                "Use only the requested workspace tools. Do not create a WorkingPlan, "
                "delegate, or store task codes or constraints in a file. A numeric "
                "input file and the final requested deliverable are the only writes. "
                "Never claim a file was read, written, or published before its tool succeeds."
            ),
            "model_id": world["model"]["id"],
            "sandbox_profile_id": world["sandbox"]["id"],
            "context_policy_id": policies["context.standard"],
            "history_policy_id": policies["history.summary_recent"],
            "memory_write_policy_id": policies["memory_write.none"],
            "memory_recall_policy_id": policies["memory_recall.none"],
            "group_enabled": False, "tool_ids": tools, "skill_ids": [],
            "bound_agent_ids": [],
            "discovery_config": {"domain_tags": ["continuous-task-uat"]},
            "collaboration_config": {"accepts_delegation": False},
        },
    }).json()
    api.request(
        "POST", f"/api/v1/agents/{created['agent']['id']}/publish", idem=True,
        json={"expected_draft_revision": created["draft"]["draft_revision"]},
    )
    return created["agent"]["id"]


def _turn(api, session_id, prompt, *, allowed_paths):
    """Run once; never conceal failures by replaying a semantic command."""
    command = api.chat(session_id, prompt)
    run_id = command["run"]["id"]
    deadline = time.monotonic() + 600
    approved = set()
    while time.monotonic() < deadline:
        run = api.request("GET", f"/api/v1/runs/{run_id}").json()
        if run["status"] in TERMINAL:
            assert run["status"] == "completed", {
                "stage": "chat", "status": run["status"],
                "terminal_reason": run.get("terminal_reason"),
            }
            return command, run, api.invocations(run_id)
        actions = api.request(
            "GET", f"/api/v1/sessions/{session_id}/required-actions",
            params={"status": "pending"},
        ).json()["items"]
        for action in actions:
            if action.get("run_id") != run_id or action["id"] in approved:
                continue
            # Exact-call approvals are restricted to this isolated workspace.
            assert action["type"] in {"tool", "session_grant", "tool_confirmation"}, (
                "Unexpected action in the bounded UAT"
            )
            assert action.get("tool_key") in TOOLS, "Unexpected tool approval"
            assert (action.get("arguments") or {}).get("path") in allowed_paths, (
                "Unexpected workspace path in tool approval"
            )
            if action["type"] == "tool_confirmation":
                api.request(
                    "POST", f"/api/v1/tool-confirmations/{action['id']}/approve",
                    idem=True, headers={"If-Match": f'W/"{action["version"]}"'},
                    json={"arguments_hash": action["arguments_hash"],
                          "reason": "Isolated continuous-task UAT exact-call approval"},
                )
            else:
                api.approve(action, reason="Isolated continuous-task UAT exact-call approval")
            approved.add(action["id"])
        time.sleep(0.5)
    raise AssertionError("A bounded UAT turn exceeded its 600-second deadline")


def _summaries(session_id):
    return _database_rows(
        "SELECT h.id, h.status, h.summary, h.source_hash, h.through_message_id, "
        "h.through_event_sequence, o.status AS command_status, o.payload_ref "
        "FROM history_summary h LEFT JOIN outbox_command o "
        "ON o.aggregate_id=h.id AND o.action='summarize_history' "
        "WHERE h.session_id=%s ORDER BY h.through_event_sequence, h.id LIMIT 30",
        (session_id,),
    )


def _await_summaries(session_id, *, minimum):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        rows = _summaries(session_id)
        assert not any(row["status"] == "failed" for row in rows), (
            "Real asynchronous history summarization failed"
        )
        if len([row for row in rows if row["status"] == "ready"]) >= minimum:
            if not any(row["status"] == "pending" for row in rows):
                return rows
        time.sleep(0.5)
    raise AssertionError("Real summaries did not advance within 600 seconds")


def _manifest(run_id):
    rows = _database_rows(
        "SELECT prompt_manifest FROM runner_v1 WHERE run_id=%s "
        "ORDER BY execution_epoch DESC, created_at DESC LIMIT 1", (run_id,),
    )
    assert rows, "Missing persisted Runner manifest"
    manifest = _json(rows[0]["prompt_manifest"])
    selections = manifest["policy_selections"]
    assert selections["context"]["policy_key"] == "context.standard"
    assert selections["history"]["policy_key"] == "history.summary_recent"
    assert selections["history"]["source_scan"]["kind"] == "complete_prefix"
    assert selections["context"]["policy_version_id"]
    assert selections["history"]["policy_version_id"]
    assert manifest["count_method"]
    assert manifest["hard_input_limit"] == 36000
    assert manifest["soft_input_target"] == 28800
    assert 0 < manifest["pre_call_count"] <= manifest["hard_input_limit"]
    return manifest


def _audit_sources(session_id, submitted):
    messages = _database_rows(
        "SELECT id, role, status, content FROM message WHERE session_id=%s "
        "AND scope_type='chat' AND status='committed' "
        "ORDER BY created_at, id LIMIT 200", (session_id,),
    )
    by_id = {message["id"]: message for message in messages}
    assert all(by_id[key]["content"] == text for key, text in submitted.items()), (
        "An original submitted Message was changed or lost"
    )
    summaries = _summaries(session_id)
    ready = {row["id"]: row for row in summaries if row["status"] == "ready"}
    positions = {message["id"]: index + 1 for index, message in enumerate(messages)}
    for row in ready.values():
        assert row["command_status"] == "completed", "Ready summary has an unsettled maintenance command"
        count = positions[row["through_message_id"]]
        canonical = [{"id": item["id"], "role": item["role"], "status": item["status"],
                      "content_hash": hashlib.sha256(item["content"].encode()).hexdigest()}
                     for item in messages[:count]]
        digest = hashlib.sha256(json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        assert digest == row["source_hash"], "Summary source prefix hash does not match original records"
        payload = _json(row["payload_ref"])
        assert payload["source_contract"] == "rolling_prefix_v1"
        assert payload["covered_message_count"] == count
        assert 0 < payload["source_message_count"] <= 200
        parent = payload["parent_summary"]
        parent_count = 0
        if parent:
            original_parent = ready[parent["id"]]
            parent_count = positions[original_parent["through_message_id"]]
            assert parent["through_message_id"] == original_parent["through_message_id"]
            assert parent["source_hash"] == original_parent["source_hash"]
            assert parent["summary_hash"] == hashlib.sha256(original_parent["summary"].encode()).hexdigest()
            assert parent_count < count
        assert payload["source_message_count"] == count - parent_count
    return ready, by_id, positions


def _audit_block(index):
    # The input is real API traffic, not a synthetic database history fixture.
    # Repetitive low-information records keep the important contract short.
    line = f"audit-batch-{index}: routine observation; no task requirement changed.\n"
    return (line * (10000 // len(line) + 1))[:10000]


def _audit_tool_continuations(run_id, numeric_input):
    rows = _database_rows(
        "SELECT c.state_snapshot FROM runner_checkpoint c JOIN runner_v1 r "
        "ON r.id=c.runner_id WHERE r.run_id=%s ORDER BY c.created_at LIMIT 100", (run_id,),
    )
    # Durable tools finish after the waiting checkpoint; their completed pair
    # is persisted in the replacement Runner input, before the next model call.
    rows += _database_rows(
        "SELECT input_snapshot AS state_snapshot FROM runner_v1 "
        "WHERE run_id=%s ORDER BY execution_epoch, created_at LIMIT 30", (run_id,),
    )
    result_count = 0
    read_preserved = False
    for row in rows:
        pending = _json(row["state_snapshot"]).get("pending_results") or []
        calls = {}
        for message in pending:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    calls[call["id"]] = call.get("name") or (call.get("function") or {}).get("name")
            if message.get("role") == "tool":
                call_id = message.get("tool_call_id") or message.get("tool_use_id")
                assert call_id in calls, "Checkpoint retained a tool result without its call"
                result_count += 1
                result = _json(message["content"])
                assert not (result.get("data") or {}).get("context_projection"), (
                    "Canonical checkpoint retained a lossy model-view tool result"
                )
                if calls[call_id] == "read_file" and (result.get("data") or {}).get("content") == numeric_input:
                    read_preserved = True
    assert result_count and read_preserved, "Canonical continuation lost the numeric read result"


@pytest.mark.timeout(3000)
def test_continuous_task_rolls_real_summaries_and_delivers_current_contract(
    phase_api, real_llm_world, record_testsuite_property,
):
    api = phase_api
    suffix = uuid.uuid4().hex[:10]
    task_code, gate = f"task-{suffix}", f"gate-{uuid.uuid4().hex[:10]}"
    result_path = f"validated-{suffix}.json"
    seed_path = "numeric-input.json"
    numeric_input = '{"values":[17,25]}'
    agent_id = _publish_agent(api, real_llm_world, suffix)
    session_id = api.create_session(agent_id)["id"]
    submitted, runs, all_calls = {}, [], []

    def send(prompt):
        command, run, calls = _turn(
            api, session_id, prompt, allowed_paths={seed_path, result_path},
        )
        submitted[command["message"]["id"]] = prompt
        runs.append(run["id"])
        all_calls.extend(calls)
        return command, run, calls

    first, _, first_calls = send(
        f"We are doing one continuing verification task. The task_code is {task_code}. "
        "The eventual deliverable is one JSON object with exactly task_code, total, unit, "
        "status, release_gate. Compute total from the numeric input file when I request "
        "delivery. Initial unit=items, status=draft, release_gate=pending, proposed "
        "filename=draft.json; later corrections override these. Do not deliver yet. "
        f"For this turn only, create {seed_path} with exactly this content: {numeric_input}. "
        "Do not save this task contract elsewhere or create a WorkingPlan. After the "
        "write succeeds reply only ACK-0. Background audit data follows:\n" + _audit_block(0)
    )
    assert any(call["tool_key"] == "write_file" and call["status"] == "succeeded"
               for call in first_calls), "First turn did not really write the numeric evidence"
    for index in range(1, 5):
        send(f"Background batch {index}. No requirement changes; no tools. Reply only ACK-{index}.\n"
             + _audit_block(index))
    send("The background batches are complete. Keep the same task contract. "
         "No tools or delivery yet; reply only CHECKPOINT-ACK.")
    first_ready = _await_summaries(session_id, minimum=1)
    first_ready_ids = {row["id"] for row in first_ready}

    # Another bounded block moves the retained recent tail beyond its budget;
    # the following assembly queues a delta against the now-ready parent.
    send("Continue the same task; no requirement changes and no tools. Reply only ACK-5.\n"
         + _audit_block(5))
    correction, _, _ = send(
        "Latest contract correction: unit must now be requests and status must be verified. "
        f"release_gate is now {gate}. The only final filename is {result_path}; never publish "
        "the old proposed filename. Preserve the original task_code and compute total from "
        "the numeric evidence. Do not deliver yet. Reply only CORRECTION-ACK, without tools."
    )
    second_ready = _await_summaries(session_id, minimum=len(first_ready_ids) + 1)
    assert any(_json(row["payload_ref"])["parent_summary"]["id"] in first_ready_ids
               for row in second_ready if _json(row["payload_ref"]).get("parent_summary")), (
        "No real second batch incorporated the frozen parent summary"
    )
    # No transcript/plan/memory escape hatch: early task identity must survive summary use.
    assert all(call["tool_key"] in TOOLS for call in all_calls)
    for call in all_calls:
        if call["tool_key"] == "write_file":
            assert call["arguments"].get("path") == seed_path
            assert call["arguments"].get("content") == numeric_input
    for run_id in runs:
        plans = api.request("GET", f"/api/v1/runs/{run_id}/working-plans").json()["items"]
        assert not plans, "Task contract was persisted in WorkingPlan, bypassing summary recall"

    final_command, final_run, final_calls = send(
        "Now finish the same continuing task. Read the numeric evidence file with the tool, "
        "compute the total, apply the latest contract correction and original task identity, "
        "write the exact requested JSON object to the latest filename, then publish that "
        "file as the one downloadable final artifact, named exactly the same as its filename. "
        "Do not ask me to repeat earlier values."
    )
    successful = {call["tool_key"] for call in final_calls if call["status"] == "succeeded"}
    assert TOOLS <= successful, "Final delivery skipped a real read/write/publish step"
    assert any(call["tool_key"] == "read_file" and call["status"] == "succeeded"
               and (call.get("result") or {}).get("data", {}).get("content") == numeric_input
               for call in final_calls), "Real read result differs from the original numeric evidence"
    _audit_tool_continuations(final_run["id"], numeric_input)
    manifests = [_manifest(run_id) for run_id in runs]
    soft_reductions = []
    for manifest in manifests:
        compaction = manifest.get("compaction") or {}
        stages = compaction.get("stages") or []
        if any(stage["action"] == "reduce_consumed_tool_results"
               or (stage["action"] == "trim_history"
                   and stage["reason"] == "provider_count_above_soft_target") for stage in stages):
            soft_reductions.append(manifest)
        elif (compaction.get("reason") == "provider_count_above_soft_target"
              and compaction.get("omitted_count", 0) > 0):
            # A later no-op recount can replace stages; cumulative omitted
            # source identities and the soft-reduction reason remain durable.
            soft_reductions.append(manifest)
    assert soft_reductions, "No actual soft-watermark tool reduction or history trimming occurred"
    final_manifest = manifests[-1]
    history = final_manifest["policy_selections"]["history"]
    assert history["summary_id"]
    assert first["message"]["id"] not in history["selected_message_ids"]
    assert correction["message"]["id"] in history["selected_message_ids"]
    assert final_command["message"]["id"] in history["excluded_message_ids"]
    assert final_command["message"]["id"] not in history["selected_message_ids"]
    assert any(section["source"] == "history_summary" for section in final_manifest["sections"])
    assert any(section.get("source_message_id") == correction["message"]["id"]
               for section in final_manifest["sections"]), "Latest correction was removed from model input"
    ready, raw, positions = _audit_sources(session_id, submitted)
    for manifest in soft_reductions:
        omitted = (manifest.get("compaction") or {}).get("omitted_sections") or []
        included = {section.get("source_message_id") for section in manifest["sections"]}
        if (manifest.get("compaction") or {}).get("omitted_count", 0) > 0:
            assert omitted and any(section.get("source_message_id") for section in omitted), (
                "Soft trimming did not retain verifiable omitted source identities"
            )
            assert (manifest["compaction"]["omitted_count"] == len(omitted))
        for section in omitted:
            source_id = section.get("source_message_id")
            if source_id is not None:
                assert source_id in raw and source_id not in included
    used = ready[history["summary_id"]]
    assert _json(used["payload_ref"])["parent_summary"], "Final Runner did not use a rolling summary"
    assert task_code in used["summary"], "Real summary lost the early task identity"
    assert all(task_code not in raw[key]["content"] for key in history["selected_message_ids"]), (
        "Early task identity remained in raw history; this would not establish summary recall"
    )
    assert history["summary_coverage"]["source_hash"] == used["source_hash"]
    assert history["summary_coverage"]["message_count"] == positions[used["through_message_id"]]
    assert history["uncovered_message_count"] >= len(history["uncovered_message_ids"])

    artifacts = api.request(
        "GET", f"/api/v1/sessions/{session_id}/artifacts", params={"status": "published"},
    ).json()["items"]
    assert len(artifacts) == 1, "The UAT must publish exactly one final artifact"
    artifact = artifacts[0]
    assert artifact["name"] == result_path
    content = api.request("GET", f"/api/v1/artifacts/{artifact['id']}/download").content
    assert hashlib.sha256(content).hexdigest() == artifact["checksum"]
    assert json.loads(content) == {"task_code": task_code, "total": 42, "unit": "requests",
                                   "status": "verified", "release_gate": gate}
    events = _database_rows(
        "SELECT payload FROM execution_event WHERE session_id=%s "
        "AND event_type='history.summary_ready' ORDER BY sequence LIMIT 30", (session_id,),
    )
    payloads = [_json(event["payload"]) for event in events]
    assert len(payloads) >= 2
    assert all(payload["model_version_id"] == real_llm_world["model_version"]["id"]
               and payload["usage"]["input_tokens"] > 0 for payload in payloads)
    record_testsuite_property(
        "continuous_task_real",
        "standard-preset,soft-watermark-reduction,two-rolling-batches,raw-prefix-hashes,"
        "recent-correction,real-tools,verified-artifact",
    )
    record_testsuite_property("continuous_task_artificial_input_budget", "36000;soft=28800")
    record_testsuite_property("continuous_task_soft_reductions", str(len(soft_reductions)))
    record_testsuite_property("continuous_task_turns", str(len(runs)))
    record_testsuite_property("continuous_task_summaries", str(len(ready)))
    record_testsuite_property("continuous_task_summary_input_tokens", str(sum(
        payload["usage"]["input_tokens"] for payload in payloads
    )))
