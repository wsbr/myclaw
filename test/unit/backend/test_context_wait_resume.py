"""Waiting preserves canonical results and resumes each Tool batch exactly once."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.foundation import UserIdentity, UserSession
from app.models.runtime_v1 import DurableRunner, Run, RunnerCheckpoint, WorkItem
from app.services.runtime_resume_service import merge_pending_results, queue_work_item_resume
from app.workers.runtime_worker import RunnerFence, RuntimeWorker


pytestmark = pytest.mark.unit


def _call(*ids, text="Original assistant decision"):
    return {"role": "assistant", "content": text, "tool_calls": [
        {"id": call_id, "type": "tool_use", "name": "execute", "input": {"command": call_id}}
        for call_id in ids
    ]}


def _result(call_id, text):
    return {"role": "tool", "tool_call_id": call_id,
            "content": json.dumps({"status": "succeeded", "summary": text, "data": {"stdout": text}})}


def _assert_complete_pairs(messages):
    pending = set()
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            assert not pending
            pending = {call["id"] for call in message["tool_calls"]}
        elif message.get("role") == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
    assert not pending


def test_parallel_wait_merge_keeps_original_arguments_and_completed_result_bytes():
    canonical = [_call("a"), _result("a", "Earlier committed side effect"),
                 _call("b", "c"), _result("c", "Synchronous result C")]
    incoming = [_call("b", "c", text=""), _result("b", "Durable result B"),
                _result("c", "Synchronous result C")]
    incoming[0]["tool_calls"][0]["input"] = {"normalized": "durable arguments"}
    originals = deepcopy((canonical, incoming))
    merged = merge_pending_results(canonical, incoming)
    _assert_complete_pairs(merged)
    assert merged[:4] == canonical
    assert merged[4] == incoming[1]
    assert len(merged) == 5
    assert (canonical, incoming) == originals


def test_completed_turn_call_id_reuse_is_not_mistaken_for_a_replayed_wait():
    first = [_call("same"), _result("same", "First turn output")]
    second = [_call("same"), _result("same", "Second turn output")]
    merged = merge_pending_results(first, second)
    assert merged == first + second
    _assert_complete_pairs(merged)


@pytest.mark.asyncio
@pytest.mark.parametrize(("explicit_continuation", "previous_checkpoint"), [
    (True, True), (True, False), (False, True),
])
async def test_synchronous_result_then_durable_wait_survives_checkpoint_and_resume(
    monkeypatch, explicit_continuation, previous_checkpoint,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.workers.runtime_worker.AsyncSessionLocal", factory)
    earlier = [_call("prior"), _result("prior", "Previous Runner committed fact")]
    complete = [_call("a"), _result("a", "UNCHANGED full result A " * 500)]
    current = [*complete, _call("b", "c"), _result("c", "UNCHANGED result C")]
    incoming = [_call("b", "c", text=""), _result("b", "Result B"), _result("c", "UNCHANGED result C")]
    principal = {"tenant_id": "tenant", "account_id": "account", "permission_mode": "direct"}
    assembly = {"agent_version_id": "agent-version", "frozen": True}
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="user", status="active"),
            UserSession(id="session", user_id="user", account_id="account", tenant_id="tenant",
                        entry_agent_id="agent", status="active", origin_type="web", permission_mode="direct"),
            Run(id="run", session_id="session", user_id="user", account_id="account", tenant_id="tenant",
                mode="chat", status="active", root_work_item_id="work"),
            WorkItem(id="work", run_id="run", session_id="session", user_id="user", tenant_id="tenant",
                     mode="chat", owner_agent_id="agent", agent_version_id="agent-version", goal="finish",
                     status="running", active_runner_id="runner", execution_epoch=1, attempt_no=1),
            DurableRunner(id="runner", work_item_id="work", run_id="run", session_id="session",
                          tenant_id="tenant", agent_id="agent", agent_version_id="agent-version",
                          execution_epoch=1, fencing_token=1, status="running", lease_token="lease",
                          principal_snapshot=principal, assembly_snapshot=assembly, prompt_manifest={},
                          input_snapshot={"pending_results": earlier, "loaded_tool_version_ids": ["prior-input-tool"]}),
        ])
        if previous_checkpoint:
            db.add(RunnerCheckpoint(
                id="previous-checkpoint", runner_id="runner", work_item_id="work",
                sequence_no=1, checkpoint_type="tool_results_persisted",
                state_snapshot={"pending_results": complete}, safe_to_resume=True,
                loaded_tool_version_ids=["previous-checkpoint-tool"],
            ))
    context = SimpleNamespace(manifest=(), compaction=None)
    hard_compactions = ({"id": "verified-summary", "summary": "Earlier committed task state",
                         "source_hash": "original-source-hash", "scope": {"run_id": "run"}},)
    result = SimpleNamespace(
        messages=[{"role": "system", "content": "platform"},
                  {"role": "user", "content": "finish"}, *earlier, *current],
        continuation_messages=tuple(current) if explicit_continuation else None,
        loaded_tool_version_ids=("newly-loaded-tool",) if explicit_continuation else (),
        hard_compactions=hard_compactions,
    )
    await RuntimeWorker("wait-context-test")._settle_waiting(
        RunnerFence("runner", "work", "run", "session", 1, "lease"),
        "tool", context, result,
    )
    async with factory.begin() as db:
        checkpoint = await db.scalar(select(RunnerCheckpoint).where(
            RunnerCheckpoint.runner_id == "runner",
        ).order_by(RunnerCheckpoint.sequence_no.desc()))
        assert checkpoint.state_snapshot["pending_results"] == current
        assert checkpoint.state_snapshot["hard_compactions"] == list(hard_compactions)
        expected_tools = ["newly-loaded-tool"] if explicit_continuation else ["previous-checkpoint-tool"]
        assert checkpoint.loaded_tool_version_ids == expected_tools
        work, run, source = await db.get(WorkItem, "work"), await db.get(Run, "run"), await db.get(DurableRunner, "runner")
        replacement = await queue_work_item_resume(
            db, work=work, run=run, source_runner=source, pending_results=incoming,
            reason="tool_result", caused_by_event_id="cause", actor_id="test",
        )
        resumed = replacement.input_snapshot["pending_results"]
        assert resumed == [*earlier, *current, incoming[1]]
        _assert_complete_pairs(resumed)
        assert replacement.principal_snapshot == principal
        assert replacement.assembly_snapshot == assembly
        assert replacement.tenant_id == "tenant"
        assert replacement.agent_version_id == "agent-version"
        assert replacement.input_snapshot["loaded_tool_version_ids"] == expected_tools
        assert replacement.input_snapshot["hard_compactions"] == list(hard_compactions)
        assert work.execution_epoch == 2
        assert source.input_snapshot["pending_results"] == earlier
    await engine.dispose()
