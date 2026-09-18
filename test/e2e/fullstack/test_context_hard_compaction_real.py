"""Opt-in real-provider UAT for synchronous hard-limit compaction.

Use the existing real_llm_world against an isolated deployment with an artificial
34096 context window, 30000 input limit and 4096 output tokens. Four real chat
commands cross that hard limit before asynchronous History summarization starts.
No Message is injected, shared policy is edited, or semantic command is replayed.
Only aggregate acceptance facts are written to JUnit properties.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from test_context_long_task_real import (
    TOOLS, _audit_block, _audit_tool_continuations, _json, _summaries, _turn,
)
from test_history_memory_real import _database_rows


pytestmark = pytest.mark.fullstack
HARD_LIMIT = 30000
SOFT_TARGET = 24000


def _publish_agent(api, world, suffix):
    capabilities = world["model_version"]["capabilities"]
    parameters = world["model_version"]["default_parameters"]
    assert capabilities.get("context_window") == 34096
    assert capabilities.get("max_input_tokens") == HARD_LIMIT
    assert parameters.get("max_output_tokens", parameters.get("max_tokens")) == 4096
    policies = world["policies"]
    history = api.request(
        "GET", f"/api/v1/policy-definitions/{policies['history.summary_recent']}",
    ).json()["default_parameters"]
    context = api.request(
        "GET", f"/api/v1/policy-definitions/{policies['context.standard']}",
    ).json()["default_parameters"]
    assert all(history.get(key) == value for key, value in {
        "token_budget": 24000, "summary_trigger_tokens": 12000,
        "summary_recent_token_budget": 8000,
    }.items()), "This corpus requires the shipped History thresholds"
    # Require the scenario-specific published defaults. Existing AgentVersions
    # retain their old policy bindings; this UAT creates a fresh private Agent.
    assert context.get("recent_history_messages") == 8
    assert context.get("recent_tool_rounds") == 3
    assert context.get("input_target_ratio", 0.8) == 0.8
    tools = [item["id"] for item in world["tools"]
             if item["enabled"] and item["tool_key"] in TOOLS]
    assert len(tools) == len(TOOLS)
    created = api.request("POST", "/api/v1/agents", json={
        "visibility": "private",
        "draft": {
            "name": f"Hard Compaction UAT {suffix}",
            "description": "Isolated real hard-limit summary and durable continuation acceptance.",
            "instructions": (
                "Complete only the current requested step, then stop. Retain the task "
                "contract across turns and apply later corrections. Background audit "
                "records are low-information data: never repeat them. Use only the "
                "requested workspace tools. Do not make a WorkingPlan, delegate, or "
                "write task codes or constraints to any file. The numeric input and "
                "the final explicitly requested deliverable are the only allowed writes. "
                "Never claim a read, write or publication before its tool succeeds."
            ),
            "model_id": world["model"]["id"],
            "sandbox_profile_id": world["sandbox"]["id"],
            "context_policy_id": policies["context.standard"],
            "history_policy_id": policies["history.summary_recent"],
            "memory_write_policy_id": policies["memory_write.none"],
            "memory_recall_policy_id": policies["memory_recall.none"],
            "group_enabled": False, "tool_ids": tools, "skill_ids": [],
            "bound_agent_ids": [],
            "discovery_config": {"domain_tags": ["hard-compaction-uat"]},
            "collaboration_config": {"accepts_delegation": False},
        },
    }).json()
    assert created["agent"]["visibility"] == "private"
    api.request(
        "POST", f"/api/v1/agents/{created['agent']['id']}/publish", idem=True,
        json={"expected_draft_revision": created["draft"]["draft_revision"]},
    )
    return created["agent"]["id"]


def _runners(run_id):
    return _database_rows(
        "SELECT id, execution_epoch, input_snapshot, prompt_manifest FROM runner_v1 "
        "WHERE run_id=%s ORDER BY execution_epoch, created_at, id LIMIT 100", (run_id,),
    )


def _manifest(runner):
    manifest = _json(runner["prompt_manifest"])
    assert manifest["hard_input_limit"] == HARD_LIMIT
    assert manifest["soft_input_target"] == SOFT_TARGET
    assert 0 < manifest["pre_call_count"] <= HARD_LIMIT
    selections = manifest["policy_selections"]
    assert selections["context"]["policy_key"] == "context.standard"
    assert selections["context"]["parameters"]["recent_history_messages"] == 8
    assert selections["context"]["parameters"]["recent_tool_rounds"] == 3
    assert selections["context"]["parameters"]["input_target_ratio"] == 0.8
    assert selections["history"]["policy_key"] == "history.summary_recent"
    assert selections["history"]["summary_id"] is None
    return manifest


def _source_messages(session_id, submitted):
    messages = _database_rows(
        "SELECT id, role, status, content FROM message WHERE session_id=%s "
        "AND scope_type='chat' AND status='committed' "
        "ORDER BY created_at, id LIMIT 100", (session_id,),
    )
    by_id = {message["id"]: message for message in messages}
    assert all(by_id[key]["content"] == text for key, text in submitted.items()), (
        "Hard compaction changed or lost an original submitted Message"
    )
    return by_id


def _verify_hard_compaction(run_id, model_version_id, first_message_id, task_code, raw):
    runners = _runners(run_id)
    assert len(runners) >= 2, "The real tool workflow did not cross a durable resume"
    manifests = [_manifest(runner) for runner in runners]
    records = [item for manifest in manifests
               for item in (manifest.get("compaction") or {}).get("hard_compactions", [])]
    assert records, "The workload never entered synchronous hard-limit compaction"
    source_hashes = {item["source_hash"] for item in records}
    assert len(source_hashes) == 1, "Durable resume compacted the same history again"
    source_hash = next(iter(source_hashes))
    first = records[0]
    assert first["version"] == 1 and first["trigger"] == "hard_input_limit"
    assert first["before_tokens"] > HARD_LIMIT
    assert 0 < first["after_tokens"] <= HARD_LIMIT
    assert first["summary_calls"] == 1, "The bounded history should need only one summary call"
    assert first["usage"]["input_tokens"] > 0 and first["usage"]["output_tokens"] > 0
    assert task_code in first["summary"], "Real hard-limit summary lost the early random identity"
    assert hashlib.sha256(first["summary"].encode()).hexdigest() == first["summary_hash"]
    assert all(item["id"] == first["id"] and item["summary_hash"] == first["summary_hash"]
               and item["summary"] == first["summary"] for item in records)
    for manifest in manifests:
        rendered = [section for section in manifest["sections"]
                    if section.get("source") == "context_compaction"]
        assert len(rendered) == 1, "A resumed request retained cache metadata without rendering its summary"
        assert rendered[0]["source_hash"] == source_hash
        assert rendered[0]["summary_hash"] == first["summary_hash"]
    source_sections = first["source_sections"]
    source_ids = {item["source_message_id"] for item in source_sections if item.get("source_message_id")}
    assert first_message_id in source_ids, "Early identity was not a source of the hard summary"
    assert source_ids <= raw.keys()
    for section in source_sections:
        message = raw.get(section.get("source_message_id"))
        if message is not None:
            digest = hashlib.sha256(json.dumps(
                {"role": message["role"], "content": message["content"]},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            assert section["checksum"] == digest, "Compaction source checksum is not the canonical message"
    assert first_message_id not in {
        section.get("source_message_id") for section in manifests[-1]["sections"]
    }, "Early identity remained as raw model input, so this would not establish summary recall"

    snapshots = [_json(runner["input_snapshot"]) for runner in runners]
    cached_inputs = [snapshot["hard_compactions"] for snapshot in snapshots
                     if snapshot.get("hard_compactions")]
    assert cached_inputs, "Resumed Runner inputs did not persist the compaction cache"
    checkpoints = _database_rows(
        "SELECT c.state_snapshot FROM runner_checkpoint c JOIN runner_v1 r ON r.id=c.runner_id "
        "WHERE r.run_id=%s ORDER BY c.created_at, c.id LIMIT 100", (run_id,),
    )
    cached_checkpoints = [_json(row["state_snapshot"])["hard_compactions"]
                          for row in checkpoints if _json(row["state_snapshot"]).get("hard_compactions")]
    assert cached_checkpoints, "Durable checkpoints did not preserve the hard summary"
    for cache in [*cached_inputs, *cached_checkpoints]:
        matches = [item for item in cache if item["source_hash"] == source_hash]
        assert len(matches) == 1 and matches[0]["summary_hash"] == first["summary_hash"]
        assert matches[0]["summary"] == first["summary"]

    turns = _database_rows(
        "SELECT id, status, context_manifest, `usage` FROM agent_turn "
        "WHERE run_id=%s ORDER BY created_at, id LIMIT 100", (run_id,),
    )
    summary_turns = [turn for turn in turns if any(
        section.get("source") == "context_compaction" and section.get("purpose") == "context_compaction"
        for section in _json(turn["context_manifest"])
    )]
    assert len(summary_turns) == 1, "Durable resumes created duplicate paid summary turns"
    summary_turn = summary_turns[0]
    metadata = next(section for section in _json(summary_turn["context_manifest"])
                    if section.get("source") == "context_compaction" and section.get("purpose") == "context_compaction")
    assert metadata["source_hash"] == source_hash and metadata["chunk_index"] == 1
    assert summary_turn["status"] == "completed"
    usage = _json(summary_turn["usage"])
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0
    assert first["usage"]["input_tokens"] == usage["input_tokens"]
    assert first["usage"]["output_tokens"] == usage["output_tokens"]
    attempts = _database_rows(
        "SELECT id, model_version_id, status, `usage` FROM provider_attempt "
        "WHERE agent_turn_id=%s ORDER BY attempt_no", (summary_turn["id"],),
    )
    paid = [attempt for attempt in attempts if attempt["status"] == "succeeded"]
    assert len(paid) == 1 and paid[0]["model_version_id"] == model_version_id
    assert _json(paid[0]["usage"])["input_tokens"] == usage["input_tokens"]
    ledger = _database_rows(
        "SELECT metric, quantity, measurement_status FROM usage_ledger_entry "
        "WHERE provider_attempt_id=%s ORDER BY metric", (paid[0]["id"],),
    )
    measured = {item["metric"]: item for item in ledger}
    assert len(measured) == len(ledger), "Summary usage was charged twice for one metric"
    assert measured["requests"]["quantity"] == 1
    for metric in ("input_tokens", "output_tokens"):
        assert measured[metric]["measurement_status"] == "measured"
        assert measured[metric]["quantity"] == usage[metric]
    return first, len(runners), len(cached_inputs), len(cached_checkpoints)


@pytest.mark.timeout(3000)
def test_real_hard_compaction_preserves_contract_and_reuses_summary_after_durable_tools(
    phase_api, real_llm_world, record_testsuite_property,
):
    api = phase_api
    suffix = uuid.uuid4().hex[:10]
    task_code = f"hard-task-{uuid.uuid4().hex[:16]}"
    gate = f"gate-{uuid.uuid4().hex[:12]}"
    seed_path, result_path = "numeric-input.json", f"hard-validated-{suffix}.json"
    numeric_input = '{"values":[17,25]}'
    agent_id = _publish_agent(api, real_llm_world, suffix)
    session_id = api.create_session(agent_id)["id"]
    submitted, runs, all_calls = {}, [], []

    def send(prompt):
        # _turn sends one semantic command, waits for settlement and handles only
        # exact workspace-call approvals. It never replays a failed chat command.
        command, run, calls = _turn(
            api, session_id, prompt, allowed_paths={seed_path, result_path},
        )
        submitted[command["message"]["id"]] = prompt
        runs.append(run["id"])
        all_calls.extend(calls)
        return command, run, calls

    first, _, first_calls = send(
        f"We are doing one continuing verification task. Its original task_code is {task_code}. "
        "The final deliverable must be one JSON object with exactly task_code, total, unit, "
        "status, release_gate. Calculate total by reading the numeric input when delivery "
        "is requested. Initially unit=items, status=draft, release_gate=pending and the "
        "proposed filename=draft.json; later corrections override those values. Do not "
        f"deliver yet. For this turn only, write {seed_path} with exactly: {numeric_input}. "
        "Store no task code or contract elsewhere, do not create a WorkingPlan, and after "
        "the successful write reply only ACK-0. Background audit data follows:\n" + _audit_block(0)
    )
    assert any(call["tool_key"] == "write_file" and call["status"] == "succeeded"
               for call in first_calls), "The numeric input was not really written"
    for index in (1, 2):
        _, _, calls = send(
            f"Background batch {index}. The contract is unchanged. No tools or delivery. "
            f"Reply only ACK-{index}.\n" + _audit_block(index)
        )
        assert not calls, "Background acknowledgement unexpectedly used a tool"
    assert not _summaries(session_id), "Asynchronous History summarization started before the hard-limit probe"
    for run_id in runs:
        for runner in _runners(run_id):
            manifest = _manifest(runner)
            compaction = manifest.get("compaction") or {}
            assert not compaction.get("hard_compactions"), "The corpus crossed the hard limit too early"
            assert not compaction.get("omitted_count"), "Soft trimming removed protected early history"
    before = _source_messages(session_id, submitted)
    assert all(task_code not in item["content"] for item in before.values()
               if item["id"] != first["message"]["id"]), "The model repeated the early identity into another message"

    # Three 10KB old blocks are about 7.5K History-estimator tokens (<12K async
    # trigger) and six user/assistant records (within the protected eight-message floor).
    # A fourth 10KB current input drives the full conservative request >30K,
    # while the ~18.9K fixed tool/policy context and current goal still fit.
    correction, final_run, final_calls = send(
        "Final contract correction and delivery instruction: unit is now requests and "
        f"status is verified; release_gate is {gate}. The only final filename is {result_path}. "
        "Preserve the original task_code; never use the old filename or old values. "
        "Now read the numeric input file, calculate total, write the exact JSON object "
        "to the final filename, and publish that file as the only downloadable artifact "
        "with its filename as the artifact name. Do not ask me to repeat earlier values. "
        "The following audit data does not change this request:\n" + _audit_block(3)
    )
    assert task_code not in submitted[correction["message"]["id"]]
    successful = {call["tool_key"] for call in final_calls if call["status"] == "succeeded"}
    assert TOOLS <= successful, "Final delivery skipped a real read/write/publish step"
    assert any(call["tool_key"] == "read_file" and call["status"] == "succeeded"
               and (call.get("result") or {}).get("data", {}).get("content") == numeric_input
               for call in final_calls)
    assert all(call["tool_key"] in TOOLS for call in all_calls)
    for call in all_calls:
        if call["tool_key"] == "write_file":
            assert call["arguments"]["path"] in {seed_path, result_path}
            if call["arguments"]["path"] == seed_path:
                assert call["arguments"]["content"] == numeric_input
    for run_id in runs:
        assert not api.request("GET", f"/api/v1/runs/{run_id}/working-plans").json()["items"]
    assert not _summaries(session_id), "An asynchronous History summary confounded the forced-compaction scenario"
    raw = _source_messages(session_id, submitted)
    assert all(raw[key] == value for key, value in before.items()), "A committed pre-compaction record changed"
    _audit_tool_continuations(final_run["id"], numeric_input)
    compaction, runner_count, cached_inputs, cached_checkpoints = _verify_hard_compaction(
        final_run["id"], real_llm_world["model_version"]["id"],
        first["message"]["id"], task_code, raw,
    )

    artifacts = api.request(
        "GET", f"/api/v1/sessions/{session_id}/artifacts", params={"status": "published"},
    ).json()["items"]
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact["name"] == result_path
    content = api.request("GET", f"/api/v1/artifacts/{artifact['id']}/download").content
    assert hashlib.sha256(content).hexdigest() == artifact["checksum"]
    assert json.loads(content) == {
        "task_code": task_code, "total": 42, "unit": "requests",
        "status": "verified", "release_gate": gate,
    }
    record_testsuite_property(
        "hard_compaction_real",
        "four-real-commands,hard-overflow,real-summary,early-random-identity,latest-correction,"
        "unchanged-sources,canonical-tool-pairs,durable-cache,single-paid-summary,verified-artifact",
    )
    record_testsuite_property("hard_compaction_artificial_input_budget", "30000;soft=24000")
    record_testsuite_property("hard_compaction_before_tokens", str(compaction["before_tokens"]))
    record_testsuite_property("hard_compaction_after_tokens", str(compaction["after_tokens"]))
    record_testsuite_property("hard_compaction_summary_calls", str(compaction["summary_calls"]))
    record_testsuite_property("hard_compaction_summary_input_tokens", str(compaction["usage"]["input_tokens"]))
    record_testsuite_property("hard_compaction_runners", str(runner_count))
    record_testsuite_property("hard_compaction_cached_inputs", str(cached_inputs))
    record_testsuite_property("hard_compaction_cached_checkpoints", str(cached_checkpoints))
