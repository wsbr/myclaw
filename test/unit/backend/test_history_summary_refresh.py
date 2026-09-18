"""Summary refresh contracts against real SQLite rows and maintenance claims."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.common import utcnow, uuid7
from app.models.foundation import ExecutionEvent, OutboxCommand, UserSession
from app.models.group_v1 import GroupMembership, GroupRun, GroupRunMember, GroupSession
from app.models.history_v1 import HistorySummary
from app.models.registry import ModelVersion, PolicyDefinition, PolicyVersion, RegisteredModel
from app.models.runtime_v1 import DurableRunner, Message, Run, WorkItem
from app.persistence.reliable_writes import ExecutionEventWriter
from app.runtime.protocol import AssistantMessageItem, ContentPart, NormalizedTurnResult, StopReason, Usage
from app.services.history_service import VisibleRecordRepository, select_history, source_hash
from app.services.maintenance_command_service import (
    MaintenanceCommandError, MaintenanceCommandWorker, build_history_result,
)


pytestmark = pytest.mark.unit


@pytest.fixture
async def history_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    parameters = {
        "token_budget": 40, "max_messages": 80,
        "summary_recent_token_budget": 20, "summary_trigger_tokens": 30,
        "summary_token_budget": 100,
    }
    session = UserSession(id=uuid7(), user_id="summary-user", entry_agent_id="agent")
    run = Run(id=uuid7(), session_id=session.id, user_id=session.user_id,
              mode="chat", status="active")
    work = WorkItem(id=uuid7(), run_id=run.id, session_id=session.id,
                    user_id=session.user_id, mode="chat", owner_agent_id="agent",
                    agent_version_id="assembly", goal="continue", status="running")
    runner = DurableRunner(id=uuid7(), work_item_id=work.id, run_id=run.id,
                          session_id=session.id, agent_id="agent", agent_version_id="assembly",
                          execution_epoch=1, status="running", principal_snapshot={}, input_snapshot={})
    model = RegisteredModel(id=uuid7(), key="summary", provider="openai_compatible",
                            model_name="summary", enabled=True)
    model_version = ModelVersion(id=uuid7(), model_id=model.id, version_no=1,
                                 provider="openai_compatible", model_name="summary",
                                 credential_ref="secret://summary", capabilities={},
                                 default_parameters={}, content_hash="m" * 64)
    policy = PolicyDefinition(id=uuid7(), policy_key="history.summary_recent", policy_type="history",
                              name="Summary", description="Summary", implementation_ref="test",
                              implementation_version="1", enabled=True, status="active")
    policy_version = PolicyVersion(id=uuid7(), policy_definition_id=policy.id,
                                   version_no=1, implementation_ref="test:v1",
                                   parameters=parameters, content_hash="p" * 64, status="published")
    async with factory.begin() as db:
        db.add_all([session, run, work, runner, model, model_version, policy, policy_version])
    captured = []

    async def invoke(_model, _credential, *, messages, parameters):
        captured.append(messages)
        return NormalizedTurnResult(
            items=(AssistantMessageItem(content_parts=(
                ContentPart(type="text", value=f"summary revision {len(captured)}"),
            )),), stop_reason=StopReason.END_TURN,
        )

    monkeypatch.setattr("app.services.maintenance_command_service.AsyncSessionLocal", factory)
    monkeypatch.setattr("app.services.maintenance_command_service.resolve_provider_credential",
                        AsyncMock(return_value="test-secret"))
    monkeypatch.setattr("app.services.maintenance_command_service._invoke", invoke)
    state = SimpleNamespace(factory=factory, session=session, run=run, runner=runner,
                            model=model, model_version=model_version, policy=policy,
                            policy_version=policy_version, parameters=parameters,
                            captured=captured, ordinal=0, started_at=utcnow())
    try:
        yield state
    finally:
        await engine.dispose()


async def append_messages(state, count, *, content=None, group_scope=None):
    messages = []
    async with state.factory.begin() as db:
        for _ in range(count):
            ordinal = state.ordinal
            state.ordinal += 1
            message_id = uuid7()
            event = await ExecutionEventWriter(db).append(
                session_id=state.session.id, event_type="message.created",
                event_dedupe_key=f"message:{message_id}", actor_type="user",
                actor_id=state.session.user_id, entity_type="message", entity_id=message_id,
                visibility="user", summary="Message", correlation_id=state.run.id,
            )
            message = Message(
                id=message_id, session_id=state.session.id, user_id=state.session.user_id,
                scope_type="group" if group_scope else "chat", sender_type="user",
                sender_user_id=state.session.user_id,
                recipient_type="agent" if group_scope else "session", role="user",
                content=content if content is not None else f"record {ordinal:03d} ".ljust(40, "x"),
                status="committed", caused_by_event_id=event.id,
                created_at=state.started_at + timedelta(seconds=ordinal),
                **(group_scope or {}),
            )
            db.add(message)
            messages.append(message)
    return messages


async def choose(state, *, exclude=(), parameters=None):
    async with state.factory.begin() as db:
        return await select_history(
            db, run=state.run, runner=state.runner, policy=state.policy,
            parameters=parameters or state.parameters, model_id=state.model.id,
            policy_version=state.policy_version, model_version_id=state.model_version.id,
            exclude_message_ids=set(exclude),
        )


async def counts(state):
    async with state.factory() as db:
        return (
            await db.scalar(select(func.count(HistorySummary.id))),
            await db.scalar(select(func.count(OutboxCommand.id))),
        )


async def seed_ready(state, prefix, *, group_scope=None):
    async with state.factory.begin() as db:
        sequence = await db.scalar(select(ExecutionEvent.sequence).where(
            ExecutionEvent.id == prefix[-1].caused_by_event_id,
        ))
        summary = HistorySummary(
            id=uuid7(), session_id=state.session.id,
            scope_type="group_agent" if group_scope else "chat",
            through_message_id=prefix[-1].id, through_event_sequence=sequence,
            source_hash=source_hash(prefix), summary="existing verified summary",
            status="ready", model_id=state.model.id, parameter_version=1,
            **(group_scope or {}),
        )
        db.add(summary)
    return summary


async def test_ready_summary_advances_and_refresh_is_idempotent(history_db):
    state = history_db
    original = await append_messages(state, 5)
    first = await choose(state)
    assert first.manifest["summary_refresh"]["status"] == "pending"
    assert first.manifest["summary_id"] is None
    worker = MaintenanceCommandWorker("summary-test")
    assert await worker.run_once()
    original += await append_messages(state, 6)

    refresh = await choose(state)
    assert refresh.summary == "summary revision 1"
    assert refresh.manifest["summary_coverage"]["through_message_id"] == original[2].id
    assert refresh.manifest["uncovered_message_ids"] == [item.id for item in original[3:9]]
    assert refresh.manifest["summary_refresh"]["through_message_id"] == original[8].id
    assert (await choose(state)).manifest["summary_refresh"]["summary_id"] == refresh.manifest["summary_refresh"]["summary_id"]
    assert await counts(state) == (2, 2)
    assert await worker.run_once()
    current = await choose(state)
    assert current.summary == "summary revision 2"
    assert current.manifest["uncovered_message_ids"] == []
    assert current.manifest["summary_refresh"]["status"] == "not_needed"
    assert len(json.loads(state.captured[0][1]["content"])) == 3
    assert json.loads(state.captured[1][1]["content"]) == {"previous_summary": "summary revision 1"}
    assert len(json.loads(state.captured[1][-1]["content"])) == 6
    async with state.factory() as db:
        rows = list(await db.scalars(select(Message).order_by(Message.created_at, Message.id)))
        assert [(row.id, row.content, row.status) for row in rows] == [
            (row.id, row.content, "committed") for row in original
        ]


async def test_current_input_exclusion_keeps_summary_hash_and_source_prefix(history_db):
    state = history_db
    messages = await append_messages(state, 10)
    ready = await seed_ready(state, messages[:2])
    # A per-Runner exclusion must never invalidate a shared ready cache.
    selected = await choose(state, exclude={messages[0].id}, parameters={
        **state.parameters, "token_budget": 1000, "summary_recent_token_budget": 1000,
    })
    assert selected.manifest["summary_id"] == ready.id
    async with state.factory() as db:
        assert (await db.get(HistorySummary, ready.id)).status == "ready"
    # An excluded active input in the middle cannot become a hole in a new
    # source hash, nor can subsequent messages be claimed as already covered.
    selected = await choose(state, exclude={messages[4].id})
    assert selected.manifest["summary_refresh"]["through_message_id"] == messages[3].id
    assert messages[4].id not in selected.manifest["selected_message_ids"]
    assert await MaintenanceCommandWorker("summary-test").run_once()
    assert [item["content"] for item in json.loads(state.captured[0][-1]["content"])] == [
        item.content for item in messages[2:4]
    ]


async def test_failed_boundary_does_not_restart_bounded_worker_retries(history_db):
    state = history_db
    await append_messages(state, 5)
    first = await choose(state)
    summary_id = first.manifest["summary_refresh"]["summary_id"]
    worker = MaintenanceCommandWorker("summary-test")
    claim = await worker.claim_one()
    await worker._settle(claim, None, MaintenanceCommandError(
        "temporary", code="provider_timeout", retryable=True,
    ))
    assert (await choose(state)).manifest["summary_refresh"]["summary_id"] == summary_id
    assert await counts(state) == (1, 1)
    async with state.factory.begin() as db:
        command = await db.get(OutboxCommand, claim.command_id)
        assert command.status == "retry_wait"
        assert command.attempt_count == 1
        command.available_at = utcnow()
    retry = await worker.claim_one()
    await worker._settle(retry, None, MaintenanceCommandError(
        "permanent", code="history_model_scope_mismatch",
    ))
    for _ in range(2):
        failed = await choose(state)
        assert failed.manifest["degraded_reason"] == "summary_failed"
        assert failed.manifest["summary_refresh"]["summary_id"] == summary_id
    assert await counts(state) == (1, 1)
    await append_messages(state, 3)
    advanced = await choose(state)
    assert advanced.manifest["summary_refresh"]["status"] == "pending"
    assert advanced.manifest["summary_refresh"]["summary_id"] != summary_id
    assert await counts(state) == (2, 2)


async def test_rolling_prefix_advances_in_bounded_batches_and_gap_is_explicit(history_db, monkeypatch):
    state = history_db
    monkeypatch.setattr("app.services.history_service.SUMMARY_SOURCE_MAX_MESSAGES", 3)
    messages = await append_messages(state, 10)
    selected = await choose(state)
    assert selected.manifest["summary_refresh"]["through_message_id"] == messages[2].id
    assert selected.manifest["degraded_reason"] == "summary_pending"
    assert await MaintenanceCommandWorker("summary-test").run_once()
    selected = await choose(state)
    assert selected.manifest["summary_coverage"]["message_count"] == 3
    assert selected.manifest["summary_refresh"]["status"] == "pending"
    assert selected.manifest["summary_refresh"]["through_message_id"] == messages[5].id
    assert selected.manifest["uncovered_message_ids"] == [item.id for item in messages[3:8]]
    assert await counts(state) == (2, 2)
    assert len(json.loads(state.captured[0][1]["content"])) == 3
    assert await MaintenanceCommandWorker("summary-test").run_once()
    assert (await choose(state)).manifest["summary_refresh"]["through_message_id"] == messages[7].id
    assert await MaintenanceCommandWorker("summary-test").run_once()
    current = await choose(state)
    assert current.manifest["summary_coverage"]["message_count"] == 8
    assert current.manifest["uncovered_message_count"] == 0
    assert [len(json.loads(request[-1]["content"])) for request in state.captured] == [3, 3, 2]


@pytest.mark.parametrize("limit_kind", ["source_bytes", "source_count", "model_window"])
async def test_worker_rejects_oversized_legacy_command_before_provider(history_db, monkeypatch, limit_kind):
    state = history_db
    messages = await append_messages(state, 3, content="x" * 1200)
    summary = await seed_ready(state, messages)
    async with state.factory.begin() as db:
        (await db.get(HistorySummary, summary.id)).status = "pending"
        if limit_kind == "model_window":
            (await db.get(ModelVersion, state.model_version.id)).capabilities = {"context_window": 500}
    if limit_kind == "source_bytes":
        monkeypatch.setattr("app.services.history_service.SUMMARY_SOURCE_MAX_BYTES", 1000)
    elif limit_kind == "source_count":
        monkeypatch.setattr("app.services.history_service.SUMMARY_SOURCE_MAX_MESSAGES", 2)
    with pytest.raises(MaintenanceCommandError) as error:
        await build_history_result({
            "history_summary_id": summary.id, "model_id": state.model.id,
            "model_version_id": state.model_version.id, "policy_definition_id": state.policy.id,
            "policy_version_id": state.policy_version.id,
        })
    assert error.value.code == "history_source_limit"
    assert state.captured == []


async def test_pending_refresh_is_coalesced_while_more_messages_arrive(history_db):
    state = history_db
    await append_messages(state, 5)
    first = await choose(state)
    await append_messages(state, 4)
    second = await choose(state)
    assert second.manifest["summary_refresh"]["summary_id"] == first.manifest["summary_refresh"]["summary_id"]
    assert await counts(state) == (1, 1)
    assert await MaintenanceCommandWorker("summary-test").run_once()
    next_refresh = await choose(state)
    assert next_refresh.manifest["summary_refresh"]["summary_id"] != first.manifest["summary_refresh"]["summary_id"]
    assert await counts(state) == (2, 2)


async def test_summary_and_outbox_creation_roll_back_together(history_db, monkeypatch):
    state = history_db
    await append_messages(state, 5)
    monkeypatch.setattr(
        "app.services.history_service.OutboxWriter.enqueue",
        AsyncMock(side_effect=RuntimeError("injected outbox failure")),
    )
    with pytest.raises(RuntimeError, match="injected outbox failure"):
        await choose(state)
    assert await counts(state) == (0, 0)


async def test_group_refresh_preserves_session_membership_and_run_scope(history_db):
    state = history_db
    group_id, member_id, group_run_id = uuid7(), uuid7(), uuid7()
    old_group_run_id, peer_member_id, other_group_id = uuid7(), uuid7(), uuid7()
    async with state.factory.begin() as db:
        state.run.mode = "group"
        state.runner.input_snapshot = {
            "group_session_id": group_id, "group_membership_id": member_id,
            "group_run_id": group_run_id,
        }
        db.add_all([
            state.run, state.runner,
            GroupSession(id=group_id, session_id=state.session.id, user_id=state.session.user_id,
                         initiator_agent_id="agent", status="open"),
            GroupMembership(id=member_id, group_session_id=group_id, agent_id="agent",
                            status="joined", invited_by_kind="system", reason="test", authorization_source="test"),
            GroupRun(id=group_run_id, run_id=state.run.id, group_session_id=group_id, goal="current"),
            GroupRun(id=old_group_run_id, run_id=uuid7(), group_session_id=group_id, goal="old"),
            GroupRunMember(group_run_id=group_run_id, agent_id="agent", membership_id=member_id),
        ])
    current_scope = {"group_session_id": group_id, "group_run_id": group_run_id,
                     "recipient_membership_id": member_id}
    messages = await append_messages(state, 10, group_scope=current_scope)
    ready = await seed_ready(state, messages[:2], group_scope={
        "group_session_id": group_id, "group_run_id": group_run_id,
        "group_membership_id": member_id,
    })
    hidden_ids = []
    for override in (
        {"group_run_id": old_group_run_id},
        {"recipient_membership_id": peer_member_id},
        {"group_session_id": other_group_id},
    ):
        hidden = await append_messages(state, 3, content="hidden source",
                                       group_scope={**current_scope, **override})
        hidden_ids.extend(item.id for item in hidden)
        await seed_ready(state, hidden, group_scope={
            "group_session_id": override.get("group_session_id", group_id),
            "group_run_id": override.get("group_run_id", group_run_id),
            "group_membership_id": override.get("recipient_membership_id", member_id),
        })
    selected = await choose(state)
    assert selected.manifest["summary_id"] == ready.id
    assert selected.manifest["summary_refresh"]["through_message_id"] == messages[7].id
    assert await MaintenanceCommandWorker("summary-test").run_once()
    projected = json.loads(state.captured[0][-1]["content"])
    assert [item["content"] for item in projected] == [item.content for item in messages[2:8]]
    assert "hidden source" not in json.dumps(projected)
    async with state.factory() as db:
        raw = await VisibleRecordRepository(db).read_for_runner(
            run=state.run, runner=state.runner,
            message_ids=[messages[0].id, *hidden_ids],
        )
        assert [message.id for message in raw] == [messages[0].id]


async def test_more_than_200_records_advance_without_resending_covered_raw_text(history_db):
    state = history_db
    messages = await append_messages(state, 405)
    worker = MaintenanceCommandWorker("summary-test")
    for expected_boundary in (199, 399, 402):
        selected = await choose(state)
        assert selected.manifest["summary_refresh"]["through_message_id"] == messages[expected_boundary].id
        assert selected.manifest["summary_refresh"]["status"] == "pending"
        assert len(selected.manifest["uncovered_message_ids"]) <= 100
        assert await worker.run_once()
    selected = await choose(state)
    assert selected.manifest["summary_coverage"]["message_count"] == 403
    assert selected.manifest["uncovered_message_count"] == 0
    assert selected.manifest["summary_refresh"]["status"] == "not_needed"
    batches = [json.loads(request[-1]["content"]) for request in state.captured]
    assert [len(batch) for batch in batches] == [200, 200, 3]
    assert [item["message_id"] for batch in batches for item in batch] == [item.id for item in messages[:403]]
    assert json.loads(state.captured[2][1]["content"])["previous_summary"] == "summary revision 2"


async def test_summary_validation_and_raw_lookup_cross_recent_projection_limit(history_db):
    state = history_db
    messages = await append_messages(state, 1007)
    ready = await seed_ready(state, messages[:1002])
    selected = await choose(state)
    assert selected.manifest["summary_id"] == ready.id
    assert selected.manifest["summary_coverage"]["message_count"] == 1002
    assert selected.manifest["summary_refresh"]["through_message_id"] == messages[1004].id
    assert await MaintenanceCommandWorker("summary-test").run_once()
    assert [item["message_id"] for item in json.loads(state.captured[0][-1]["content"])] == [
        item.id for item in messages[1002:1005]
    ]
    async with state.factory.begin() as db:
        raw = await VisibleRecordRepository(db).read_for_runner(
            run=state.run, runner=state.runner, message_ids=[messages[0].id],
        )
        assert len(raw) == 1 and raw[0].content == messages[0].content
        raw[0].content = "the earliest source was corrected"
    changed = await choose(state)
    assert changed.summary is None
    assert changed.manifest["degraded_reason"] == "summary_source_changed"


async def test_oversized_record_is_sliced_and_covered_only_after_all_slices(history_db):
    state = history_db
    original = "证据与决策" * 6000
    large = (await append_messages(state, 1, content=original))[0]
    await append_messages(state, 2)
    queued = await choose(state)
    summary_id = queued.manifest["summary_refresh"]["summary_id"]
    assert queued.manifest["summary_id"] is None
    assert queued.manifest["uncovered_message_ids"] == [large.id]
    assert await MaintenanceCommandWorker("summary-test").run_once()
    records = [json.loads(request[-1]["content"])[0] for request in state.captured]
    assert 1 < len(records) <= 8
    assert "".join(record["content"] for record in records) == original
    assert records[0]["start_char"] == 0
    assert records[-1]["end_char"] == len(original)
    assert all(left["end_char"] == right["start_char"] for left, right in zip(records, records[1:]))
    assert all(record["message_id"] == large.id for record in records)
    assert len({record["source_content_hash"] for record in records}) == 1
    assert all(len(request[-1]["content"].encode("utf-8")) <= 48_000 for request in state.captured)
    selected = await choose(state)
    assert selected.manifest["summary_id"] == summary_id
    assert selected.manifest["summary_coverage"]["through_message_id"] == large.id
    assert selected.manifest["uncovered_message_count"] == 0
    async with state.factory() as db:
        assert (await db.get(Message, large.id)).content == original


async def test_record_above_command_limit_remains_explicitly_uncovered(history_db):
    state = history_db
    large = (await append_messages(state, 1, content="x" * 192_001))[0]
    await append_messages(state, 2)
    selected = await choose(state)
    assert selected.manifest["summary_refresh"]["status"] == "source_limit"
    assert selected.manifest["degraded_reason"] == "summary_source_limit"
    assert selected.manifest["uncovered_range"] == {"first_message_id": large.id, "last_message_id": large.id}
    assert selected.summary is None
    assert await counts(state) == (0, 0)


async def test_slice_call_limit_fails_before_provider_and_does_not_fake_coverage(history_db, monkeypatch):
    state = history_db
    monkeypatch.setattr("app.services.history_service.SUMMARY_COMMAND_MAX_CALLS", 1)
    await append_messages(state, 1, content="x" * 60_000)
    await append_messages(state, 2)
    queued = await choose(state)
    assert await MaintenanceCommandWorker("summary-test").run_once()
    assert not state.captured
    selected = await choose(state)
    assert selected.summary is None
    assert selected.manifest["summary_refresh"]["status"] == "failed"
    assert selected.manifest["summary_refresh"]["summary_id"] == queued.manifest["summary_refresh"]["summary_id"]
    async with state.factory() as db:
        command = await db.scalar(select(OutboxCommand))
        assert command.status == "dead" and command.last_error == "history_source_limit"


@pytest.mark.parametrize("changed", ["parent_text", "covered_source", "delta_source"])
async def test_parent_and_raw_source_changes_reject_queued_refresh(history_db, changed):
    state = history_db
    messages = await append_messages(state, 8)
    parent = await seed_ready(state, messages[:2])
    selected = await choose(state)
    async with state.factory.begin() as db:
        if changed == "parent_text":
            (await db.get(HistorySummary, parent.id)).summary = "changed cache text"
        else:
            index = 0 if changed == "covered_source" else 3
            (await db.get(Message, messages[index].id)).content = "changed original"
    assert await MaintenanceCommandWorker("summary-test").run_once()
    assert not state.captured
    async with state.factory() as db:
        child = await db.get(HistorySummary, selected.manifest["summary_refresh"]["summary_id"])
        command = await db.scalar(select(OutboxCommand))
        assert child.status == "failed"
        assert command.last_error == ("history_parent_changed" if changed == "parent_text" else "history_source_changed")


async def test_source_change_during_provider_call_is_rejected_at_settlement(history_db, monkeypatch):
    state = history_db
    messages = await append_messages(state, 5)
    queued = await choose(state)

    async def mutate_source(_model, _credential, *, messages, parameters):
        async with state.factory.begin() as db:
            source_id = json.loads(messages[-1]["content"])[0]["message_id"]
            (await db.get(Message, source_id)).content = "changed while the provider was running"
        return NormalizedTurnResult(items=(AssistantMessageItem(content_parts=(
            ContentPart(type="text", value="now stale summary"),
        )),), stop_reason=StopReason.END_TURN)

    monkeypatch.setattr("app.services.maintenance_command_service._invoke", mutate_source)
    assert await MaintenanceCommandWorker("summary-test").run_once()
    async with state.factory() as db:
        summary = await db.get(HistorySummary, queued.manifest["summary_refresh"]["summary_id"])
        command = await db.scalar(select(OutboxCommand))
        assert summary.status == "failed" and summary.summary is None
        assert command.status == "dead" and command.last_error == "history_source_changed"


async def test_recent_policy_uses_one_bounded_source_query(history_db, monkeypatch):
    state = history_db
    await append_messages(state, 1001)
    state.policy.policy_key = "history.recent"
    calls = []
    original = VisibleRecordRepository.messages_for_runner

    async def recent(repository, **kwargs):
        calls.append(True)
        return await original(repository, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("history.recent must not stream complete history")

    monkeypatch.setattr(VisibleRecordRepository, "messages_for_runner", recent)
    monkeypatch.setattr(VisibleRecordRepository, "iter_for_runner", forbidden)
    selected = await choose(state)
    assert len(calls) == 1
    assert selected.manifest["source_scan"] == {
        "kind": "recent_window", "message_count": 1000,
        "max_messages": 1000, "may_omit_older": True,
    }
    assert selected.manifest["uncovered_message_count"] == 996
    assert selected.manifest["uncovered_ids_truncated"] is True
    assert await counts(state) == (0, 0)


async def test_partial_slice_failure_keeps_parent_coverage_and_accumulates_usage(history_db, monkeypatch):
    state = history_db
    first = await append_messages(state, 1)
    parent = await seed_ready(state, first)
    large = (await append_messages(state, 1, content="x" * 60_000))[0]
    await append_messages(state, 2)
    selected = await choose(state)
    calls = []

    async def partial(_model, _credential, *, messages, parameters):
        calls.append(messages)
        if len(calls) == 2:
            raise MaintenanceCommandError("second slice failed", code="provider_invalid", usage={
                "input_tokens": 2, "output_tokens": 1, "cached_input_tokens": 0,
            })
        return NormalizedTurnResult(items=(AssistantMessageItem(content_parts=(
            ContentPart(type="text", value="partial update"),
        )),), stop_reason=StopReason.END_TURN, usage=Usage(input_tokens=5, output_tokens=3))

    monkeypatch.setattr("app.services.maintenance_command_service._invoke", partial)
    assert await MaintenanceCommandWorker("summary-test").run_once()
    current = await choose(state)
    assert current.manifest["summary_id"] == parent.id
    assert current.manifest["summary_coverage"]["message_count"] == 1
    assert current.manifest["uncovered_message_ids"] == [large.id]
    assert current.manifest["summary_refresh"]["status"] == "failed"
    async with state.factory() as db:
        child = await db.get(HistorySummary, selected.manifest["summary_refresh"]["summary_id"])
        event = await db.scalar(select(ExecutionEvent).where(ExecutionEvent.event_type == "history.summary_failed"))
        assert child.status == "failed" and child.summary is None
        assert event.payload["usage"] == {"input_tokens": 7, "output_tokens": 4, "cached_input_tokens": 0}
