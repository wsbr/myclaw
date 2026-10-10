from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.channel_v1 import NotificationDeliveryAttempt
from app.models.foundation import IdempotencyRecord, OutboxCommand, PlatformAuditEvent
from app.models.observability_v1 import ObservabilitySpan, ObservabilitySpanLink, RunnerTrace
from app.models.runtime_v1 import Run, RunnerCheckpoint, WorkerInstance, WorkItem
from app.services.retention_service import run_retention_cleanup


pytestmark = pytest.mark.unit


def _outbox(command_id: str, *, status: str, when: datetime) -> OutboxCommand:
    return OutboxCommand(
        id=command_id, topic="runtime.runner", action="start_runner",
        aggregate_type="runner", aggregate_id=command_id, partition_key=command_id,
        dedupe_key=command_id, payload_ref={}, priority=100, available_at=when,
        status=status, attempt_count=1, completed_at=when if status == "completed" else None,
        created_at=when, updated_at=when,
    )


@pytest.mark.asyncio
async def test_retention_cleanup_prunes_only_expired_operational_rows():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 2, 12, 0, 0)
    old_31 = now - timedelta(days=31)
    old_91 = now - timedelta(days=91)
    old_181 = now - timedelta(days=181)

    async with factory.begin() as db:
        db.add_all([
            IdempotencyRecord(
                id="idem-old", principal_type="account", principal_id="p", operation="op",
                idempotency_key="old", request_hash="a" * 64, expires_at=old_31,
            ),
            IdempotencyRecord(
                id="idem-live", principal_type="account", principal_id="p", operation="op",
                idempotency_key="live", request_hash="b" * 64, expires_at=now + timedelta(days=1),
            ),
            _outbox("outbox-completed-old", status="completed", when=old_31),
            _outbox("outbox-completed-live", status="completed", when=now - timedelta(days=1)),
            _outbox("outbox-dead-old", status="dead", when=old_181),
            WorkerInstance(
                worker_id="runtime:old", worker_kind="runtime", capacity=8,
                active_slots=0, status="stopped", hostname="old", process_id=1,
                release_id="old", started_at=old_31, heartbeat_at=old_31,
                stopped_at=old_31, created_at=old_31, updated_at=old_31,
            ),
            WorkerInstance(
                worker_id="runtime:live", worker_kind="runtime", capacity=8,
                active_slots=0, status="active", hostname="live", process_id=2,
                release_id="current", started_at=now, heartbeat_at=now,
                created_at=now, updated_at=now,
            ),
        ])
        db.add(Run(
            id="run", session_id="session", user_id="user", mode="chat",
            status="completed", completed_at=old_31, created_at=old_31, updated_at=old_31,
        ))
        work = WorkItem(
            id="work-old", run_id="run", session_id="session", user_id="user", mode="chat",
            owner_agent_id="agent", agent_version_id="version", goal="done", status="succeeded",
            execution_epoch=1, attempt_no=1, updated_at=old_31, created_at=old_31,
        )
        db.add(work)
        db.add(RunnerCheckpoint(
            id="checkpoint-old", runner_id="runner", work_item_id=work.id, sequence_no=1,
            checkpoint_type="settled", state_snapshot={}, safe_to_resume=False, created_at=old_31,
        ))
        trace = RunnerTrace(
            id="trace-old", trace_id="a" * 32, root_span_id="b" * 16, runner_id="runner",
            session_id="session", run_id="run", work_item_id=work.id, agent_id="agent",
            status="completed", sampled=True, started_at=old_31, ended_at=old_31, created_at=old_31,
        )
        span = ObservabilitySpan(
            id="span-old", runner_trace_id=trace.id, span_id="c" * 16,
            parent_span_id=trace.root_span_id, name="runner_execution", span_kind="runtime",
            status="ok", started_at=old_31, ended_at=old_31, duration_ms=1, attributes={},
        )
        db.add_all([trace, span, ObservabilitySpanLink(
            id="link-old", span_id=span.id, linked_trace_id="d" * 32,
            linked_span_id="e" * 16, link_type="agent_invocation", created_at=old_31,
        )])
        db.add(NotificationDeliveryAttempt(
            id="delivery-attempt-old", delivery_id="delivery", attempt_no=1, status="failed",
            claim_token="claim", request_metadata={}, retryable=False,
            created_at=old_91, updated_at=old_91,
        ))

    async with factory() as db:
        result = await run_retention_cleanup(db, now=now, batch_size=100, job_id="job-1")

    assert result.idempotency_records == 1
    assert result.completed_outbox_commands == 1
    assert result.dead_outbox_commands == 1
    assert result.runner_checkpoints == 1
    assert result.runner_traces == 1
    assert result.observability_spans == 1
    assert result.observability_span_links == 1
    assert result.notification_delivery_attempts == 1
    assert result.worker_instances == 1

    async with factory() as db:
        assert await db.get(IdempotencyRecord, "idem-live") is not None
        assert await db.get(OutboxCommand, "outbox-completed-live") is not None
        assert await db.get(WorkItem, "work-old") is not None
        assert await db.get(WorkerInstance, "runtime:old") is None
        assert await db.get(WorkerInstance, "runtime:live") is not None
        audit = await db.scalar(select(PlatformAuditEvent).where(
            PlatformAuditEvent.dedupe_key == "retention_cleanup:job-1",
        ))
        assert audit is not None
        assert audit.changes["deleted_counts"]["runner_traces"] == 1
        assert await db.scalar(select(func.count()).select_from(PlatformAuditEvent)) == 9
        assert not result.failures
    await engine.dispose()


@pytest.mark.asyncio
async def test_retention_category_failure_does_not_rollback_other_categories():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text(
            "CREATE TRIGGER block_checkpoint_delete BEFORE DELETE ON runner_checkpoint "
            "BEGIN SELECT RAISE(ABORT, 'checkpoint protected'); END"
        ))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 2, 12)
    old = now - timedelta(days=31)
    async with factory.begin() as db:
        db.add_all([
            Run(id="run", session_id="session", user_id="user", mode="chat",
                status="completed", completed_at=old, created_at=old, updated_at=old),
            WorkItem(id="work", run_id="run", session_id="session", user_id="user",
                     mode="chat", owner_agent_id="agent", agent_version_id="version",
                     goal="done", status="succeeded", execution_epoch=1, attempt_no=1,
                     created_at=old, updated_at=old),
            RunnerCheckpoint(id="checkpoint", runner_id="runner", work_item_id="work",
                             sequence_no=1, checkpoint_type="settled", state_snapshot={},
                             safe_to_resume=False, created_at=old),
            _outbox("completed", status="completed", when=old),
            WorkerInstance(worker_id="worker", worker_kind="runtime", capacity=1,
                           active_slots=0, status="stopped", hostname="old", process_id=1,
                           release_id="old", started_at=old, heartbeat_at=old,
                           stopped_at=old, created_at=old, updated_at=old),
        ])
    async with factory() as db:
        result = await run_retention_cleanup(db, now=now, job_id="partial")
    assert result.failures == {"checkpoints": "IntegrityError"}
    assert result.runner_checkpoints == 0
    assert result.completed_outbox_commands == result.worker_instances == 1
    async with factory() as db:
        assert await db.get(RunnerCheckpoint, "checkpoint") is not None
        assert await db.get(OutboxCommand, "completed") is None
        assert await db.get(WorkerInstance, "worker") is None
        assert await db.scalar(select(PlatformAuditEvent).where(
            PlatformAuditEvent.action == "retention.checkpoint_batch",
        )) is None  # The allowlist rolls back together with the failed delete.
        summary = await db.scalar(select(PlatformAuditEvent).where(
            PlatformAuditEvent.dedupe_key == "retention_cleanup:partial",
        ))
        assert summary.outcome == "failed"
        assert summary.event_metadata["failure"] == {"checkpoints": "IntegrityError"}
        assert summary.changes["deleted_counts"]["worker_instances"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_retention_cleanup_is_bounded_per_category():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 2, 12, 0, 0)
    async with factory.begin() as db:
        for index in range(3):
            db.add(IdempotencyRecord(
                id=f"idem-{index}", principal_type="account", principal_id="p", operation="op",
                idempotency_key=str(index), request_hash=str(index) * 64,
                expires_at=now - timedelta(days=1),
            ))
    async with factory() as db:
        result = await run_retention_cleanup(db, now=now, batch_size=1, job_id="job-bounded")
    assert result.idempotency_records == 1
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(IdempotencyRecord)) == 2
    await engine.dispose()
