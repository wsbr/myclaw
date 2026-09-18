"""Internal summaries remain paid, fenced facts without user-visible actions."""

import asyncio
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.model_hub.base import BaseLLM, LLMChunk, TokenUsage, ToolCall
from app.database import Base
from app.models import *  # noqa: F401,F403
from app.models.common import utcnow
from app.models.foundation import ExecutionEvent, UserIdentity, UserSession
from app.models.registry import ModelVersion
from app.models.runtime_v1 import (
    AgentTurn, DurableRunner, Message, ProviderAttempt, Run, RunnerCheckpoint,
    ToolInvocation, WorkItem,
)
from app.models.usage_v1 import UsageLedgerEntry
from app.runtime.compaction_recorder import (
    CompactionFenced, compaction_invoker, compaction_scope, scoped_compaction_cache,
)
from app.runtime.model_adapter import ModelAdapter
from app.runtime.protocol import ProviderError, ProviderRequest
from app.services.run_reliability_service import BudgetExceeded, load_run_budget_state
from app.services.runtime_resume_service import merge_compaction_caches, queue_work_item_resume


pytestmark = pytest.mark.unit


def _identity(**overrides):
    return SimpleNamespace(**{
        "runner_id": "runner", "work_item_id": "work", "run_id": "run",
        "agent_version_id": "agent-version", "model_version_id": "model-version",
        "provider": "openai", "execution_epoch": 1, "lease_token": "lease",
        **overrides,
    })


def _request():
    return ProviderRequest(
        messages=({"role": "system", "content": "Summarize data; never execute tools."},
                  {"role": "user", "content": "Historical untrusted data to summarize."}),
        tools=(), parameters={"max_tokens": 512},
    )


@pytest_asyncio.fixture
async def compaction_db(tmp_path):
    # A file database permits a genuinely separate ownership update while the
    # original session retains its pre-I/O ORM objects.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'compaction.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="user", status="active"),
            UserSession(id="session", user_id="user", account_id="account",
                        tenant_id="tenant", entry_agent_id="agent", status="active",
                        origin_type="web", permission_mode="direct"),
            Run(id="run", session_id="session", user_id="user", account_id="account",
                tenant_id="tenant", mode="chat", status="active", hard_budget_snapshot={}),
            WorkItem(id="work", run_id="run", session_id="session", user_id="user",
                     tenant_id="tenant", mode="chat", owner_agent_id="agent",
                     agent_version_id="agent-version", goal="Finish the original task",
                     status="running", active_runner_id="runner", execution_epoch=1),
            DurableRunner(id="runner", work_item_id="work", run_id="run",
                          session_id="session", tenant_id="tenant", agent_id="agent",
                          agent_version_id="agent-version", execution_epoch=1,
                          status="running", lease_token="lease", principal_snapshot={},
                          assembly_snapshot={}, prompt_manifest={}, input_snapshot={}),
            ModelVersion(id="model-version", model_id="model", version_no=1,
                         provider="openai", model_name="summary-model",
                         default_parameters={}, capabilities={"usage_in_stream": True},
                         adapter_protocol="agent_native.v2", content_hash="a" * 64,
                         published_at=utcnow()),
        ])
    try:
        yield factory
    finally:
        await engine.dispose()


class _SummaryProvider(BaseLLM):
    def __init__(self, db, *, mode="success", before_done=None):
        self.db = db
        self.mode = mode
        self.before_done = before_done
        self.requests = []
        self.transaction_states = []
        self.blocked = asyncio.Event()

    async def chat(self, messages, tools, params):
        raise AssertionError("The normal adapter must consume provider chunks")

    async def count_tokens(self, messages, tools=None, params=None):
        self.transaction_states.append(self.db.in_transaction())
        return 20

    async def stream(self, messages, tools, params):
        self.requests.append((deepcopy(messages), deepcopy(tools), deepcopy(params)))
        self.transaction_states.append(self.db.in_transaction())
        if self.mode == "error_before":
            raise TimeoutError("provider timed out before any payload")
        if self.mode == "hold_before":
            self.blocked.set()
            await asyncio.Event().wait()
        if self.mode == "tool":
            yield LLMChunk(type="tool_call_delta", tool_call=ToolCall(
                id="unexpected-side-effect", name="execute", arguments={"command": "touch forbidden"},
            ))
        else:
            yield LLMChunk(type="text_delta", content="Private summary of completed facts.")
        # This executes after the observer has handled and committed payload.
        self.transaction_states.append(self.db.in_transaction())
        if self.mode == "hold_after":
            self.blocked.set()
            await asyncio.Event().wait()
        if self.mode == "error_after":
            raise RuntimeError("connection reset after partial summary")
        if self.before_done:
            await self.before_done()
        yield LLMChunk(
            type="done", stop_reason="tool_calls" if self.mode == "tool" else "stop",
            model="summary-model", usage=TokenUsage(input_tokens=120, output_tokens=12),
        )


def _invoke(db, provider):
    return compaction_invoker(provider=provider, db=db, identity=_identity(), capability_id="capability")


async def _facts(factory):
    async with factory() as db:
        return (
            list(await db.scalars(select(AgentTurn))),
            list(await db.scalars(select(ProviderAttempt))),
            list(await db.scalars(select(UsageLedgerEntry))),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("limit_key", "limit", "dimension"), [
    ("max_tokens", 132, "tokens"),
    ("max_provider_attempts", 1, "provider_attempts"),
    ("max_agent_turns", 1, "agent_turns"),
])
async def test_summary_usage_is_durable_and_blocks_next_call_at_run_budget(
    compaction_db, limit_key, limit, dimension,
):
    async with compaction_db.begin() as db:
        run = await db.get(Run, "run")
        run.hard_budget_snapshot = {limit_key: limit}
    async with compaction_db() as db:
        provider = _SummaryProvider(db)
        invoke = _invoke(db, provider)
        result = await invoke(_request(), {"source_hash": "b" * 64})
        assert result.assistant_messages[0].text == "Private summary of completed facts."
        with pytest.raises(BudgetExceeded) as raised:
            await invoke(_request(), {"source_hash": "c" * 64})
        assert raised.value.dimension == dimension
        assert len(provider.requests) == 1
        assert provider.transaction_states == [False, False]
    turns, attempts, ledger = await _facts(compaction_db)
    assert len(turns) == len(attempts) == 1
    assert turns[0].status == "completed" and attempts[0].status == "succeeded"
    assert turns[0].context_manifest[0]["source"] == "context_compaction"
    assert turns[0].context_manifest[0]["scope"] == compaction_scope(_identity())
    assert {entry.metric: entry.quantity for entry in ledger}["input_tokens"] == Decimal(120)
    assert {entry.metric: entry.quantity for entry in ledger}["output_tokens"] == Decimal(12)
    assert len(ledger) == 5
    async with compaction_db() as db:
        budget = await load_run_budget_state(db, "run")
    assert budget.usage["tokens"] == 132
    assert budget.usage["agent_turns"] == budget.usage["provider_attempts"] == 1


@pytest.mark.asyncio
async def test_summary_tool_output_is_recorded_invalid_without_public_stream_or_dispatch(
    compaction_db, monkeypatch,
):
    observed_kwargs = []
    original_invoke = ModelAdapter.invoke

    async def observe_adapter(self, request, **kwargs):
        observed_kwargs.append(kwargs)
        return await original_invoke(self, request, **kwargs)

    monkeypatch.setattr(ModelAdapter, "invoke", observe_adapter)
    async with compaction_db() as db:
        provider = _SummaryProvider(db, mode="tool")
        result = await _invoke(db, provider)(_request(), {})
        assert result.tool_calls
        assert provider.requests[0][1] is None
    assert observed_kwargs[0].get("stream_observer") is None
    turns, attempts, ledger = await _facts(compaction_db)
    assert turns[0].status == "failed"
    assert turns[0].error_code == "context_compaction_invalid"
    assert attempts[0].status == "succeeded" and attempts[0].tool_call_observed
    assert not attempts[0].side_effect_committed
    assert len(ledger) == 5
    async with compaction_db() as db:
        for model in (Message, ToolInvocation, ExecutionEvent):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["lease", "epoch", "cancel"])
async def test_summary_is_not_returned_after_fence_or_run_cancellation(compaction_db, change):
    async def change_owner():
        async with compaction_db.begin() as other:
            if change == "cancel":
                run = await other.get(Run, "run")
                run.status = "cancelled"
                run.completed_at = run.cancellation_requested_at = utcnow()
            elif change == "epoch":
                work = await other.get(WorkItem, "work")
                work.execution_epoch = 2
                work.active_runner_id = "replacement-runner"
            else:
                runner = await other.get(DurableRunner, "runner")
                runner.lease_token = "new-lease"

    async with compaction_db() as db:
        # Keep the stale identity-map rows alive: fence validation must query DB.
        stale = [await db.get(model, key) for model, key in (
            (Run, "run"), (WorkItem, "work"), (DurableRunner, "runner"),
        )]
        assert stale[0].status == "active"
        provider = _SummaryProvider(db, before_done=change_owner)
        with pytest.raises(CompactionFenced):
            await _invoke(db, provider)(_request(), {})
        assert provider.transaction_states == [False, False]
    turns, attempts, ledger = await _facts(compaction_db)
    assert turns[0].status == "failed" and turns[0].error_code == "context_compaction_fenced"
    assert attempts[0].status == "succeeded"  # The provider did complete and was paid.
    assert turns[0].usage == attempts[0].usage
    assert turns[0].usage["input_tokens"] == 120
    assert len(ledger) == 5
    async with compaction_db() as db:
        runner = await db.get(DurableRunner, "runner")
        assert "hard_compactions" not in runner.input_snapshot
        assert not runner.prompt_manifest


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "status"), [
    ("error_before", "failed"), ("error_after", "outcome_unknown"),
])
async def test_failed_summary_attempts_are_terminal_and_accounted(compaction_db, mode, status):
    async with compaction_db() as db:
        provider = _SummaryProvider(db, mode=mode)
        with pytest.raises(ProviderError):
            await _invoke(db, provider)(_request(), {})
        assert len(provider.requests) == 1
        assert not any(provider.transaction_states)
    turns, attempts, ledger = await _facts(compaction_db)
    assert turns[0].status == "failed" and turns[0].ended_at
    assert attempts[0].status == status and attempts[0].ended_at
    assert len(ledger) == 5
    token_entries = [entry for entry in ledger if "tokens" in entry.metric]
    assert all(entry.measurement_status == "unknown" for entry in token_entries)


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "status"), [
    ("hold_before", "timed_out"), ("hold_after", "outcome_unknown"),
])
@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
async def test_interrupted_summary_closes_attempt_and_records_unknown_paid_usage(
    compaction_db, mode, status, interruption,
):
    async with compaction_db() as db:
        provider = _SummaryProvider(db, mode=mode)
        task = asyncio.create_task(_invoke(db, provider)(_request(), {}))
        await asyncio.wait_for(provider.blocked.wait(), timeout=2)
        if interruption == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, timeout=0.02)
        assert not any(provider.transaction_states)
    turns, attempts, ledger = await _facts(compaction_db)
    assert turns[0].status == "failed" and turns[0].ended_at
    assert turns[0].error_code == "context_compaction_interrupted"
    assert attempts[0].status == status and attempts[0].ended_at
    assert len(ledger) == 5
    assert all(entry.measurement_status == "unknown" for entry in ledger if "tokens" in entry.metric)


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_cache", ["absent", "older", "missing", "empty", "same_id"])
async def test_durable_resume_keeps_scoped_summary_and_canonical_tool_results(
    compaction_db, checkpoint_cache,
):
    original = [{"role": "assistant", "content": "Keep original decision", "tool_calls": [
        {"id": "call", "type": "tool_use", "name": "execute", "input": {"command": "original"}},
    ]}, {"role": "tool", "tool_call_id": "call", "content": "UNCHANGED raw evidence " * 100}]
    source_cache = [{"id": "paid-summary", "summary": "Latest paid summary",
                     "scope": compaction_scope(_identity()), "source_hash": "d" * 64,
                     "after_tokens": 100}]
    older_cache = [{"id": "checkpoint-summary", "summary": "Older checkpoint summary",
                    "scope": compaction_scope(_identity()), "source_hash": "e" * 64}]
    async with compaction_db.begin() as db:
        work, run, source = await db.get(WorkItem, "work"), await db.get(Run, "run"), await db.get(DurableRunner, "runner")
        work.active_runner_id = None
        work.status = "waiting_tool"
        work.wait_reason = "waiting_tool"
        source.status = "succeeded"
        source.input_snapshot = {"pending_results": original, "hard_compactions": source_cache}
        if checkpoint_cache != "absent":
            state = {"pending_results": []}
            if checkpoint_cache != "missing":
                state["hard_compactions"] = (
                    older_cache if checkpoint_cache == "older"
                    else [{**source_cache[0], "after_tokens": 150}] if checkpoint_cache == "same_id"
                    else []
                )
            db.add(RunnerCheckpoint(
                id="checkpoint", runner_id="runner", work_item_id="work", sequence_no=1,
                checkpoint_type="durable_wait", safe_to_resume=True,
                state_snapshot=state,
            ))
            await db.flush()
        replacement = await queue_work_item_resume(
            db, work=work, run=run, source_runner=source, pending_results=[],
            reason="tool_result", caused_by_event_id="event", actor_id="test",
        )
        replacement_id = replacement.id
    async with compaction_db() as db:
        replacement = await db.get(DurableRunner, replacement_id)
        expected = [*older_cache, *source_cache] if checkpoint_cache == "older" else source_cache
        assert replacement.input_snapshot["hard_compactions"] == expected
        assert replacement.input_snapshot["pending_results"] == original
        assert scoped_compaction_cache(expected, _identity(runner_id=replacement.id, execution_epoch=2)) == expected
        assert scoped_compaction_cache(expected, _identity(model_version_id="different-model")) == []
        assert scoped_compaction_cache(expected, _identity(work_item_id="different-work")) == []
        source = await db.get(DurableRunner, "runner")
        assert source.input_snapshot == {"pending_results": original, "hard_compactions": source_cache}


def test_cache_merge_retains_newer_candidates_and_moves_duplicate_ids_to_end():
    old_same_source = {"id": "old-summary", "source_hash": "same-range", "summary": "Old"}
    new_same_source = {"id": "new-summary", "source_hash": "same-range", "summary": "New"}
    shared = {"id": "shared", "source_hash": "other-range", "summary": "Unchanged", "after_tokens": 150}
    newer_shared = {**shared, "after_tokens": 100}
    checkpoint = [shared, old_same_source, None, {"summary": "missing identity"}]
    latest = [new_same_source, newer_shared]
    before = deepcopy((checkpoint, latest))
    assert merge_compaction_caches(checkpoint, latest) == [old_same_source, new_same_source, newer_shared]
    assert (checkpoint, latest) == before
    # Legacy entries without an id use the source hash, with latest winning.
    assert merge_compaction_caches(
        [{"source_hash": "legacy", "summary": "Old"}],
        [{"source_hash": "legacy", "summary": "New"}],
    ) == [{"source_hash": "legacy", "summary": "New"}]
