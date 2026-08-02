"""Bounded retention cleanup for disposable operational records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.channel_v1 import NotificationDeliveryAttempt
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord, OutboxCommand, PlatformAuditEvent
from app.models.observability_v1 import ObservabilitySpan, ObservabilitySpanLink, RunnerTrace
from app.models.runtime_v1 import RunnerCheckpoint, WorkItem


TERMINAL_WORK_STATUSES = {
    "succeeded", "failed", "timed_out", "cancelled", "abandoned", "blocked",
}
TERMINAL_DELIVERY_ATTEMPT_STATUSES = {
    "succeeded", "failed", "timed_out", "outcome_unknown",
}


@dataclass
class RetentionResult:
    idempotency_records: int = 0
    completed_outbox_commands: int = 0
    dead_outbox_commands: int = 0
    runner_checkpoints: int = 0
    observability_span_links: int = 0
    observability_spans: int = 0
    runner_traces: int = 0
    notification_delivery_attempts: int = 0


async def _ids(db: AsyncSession, statement, limit: int) -> list[str]:
    return list(await db.scalars(statement.limit(limit)))


async def run_retention_cleanup(
    db: AsyncSession, *, now: datetime | None = None, batch_size: int | None = None,
    job_id: str | None = None,
) -> RetentionResult:
    """Delete only records declared disposable by the phase-one retention rules.

    Session, Run, WorkItem, ExecutionEvent, Artifact and audit history are never
    deleted here. Every category is bounded independently so one backlog cannot
    starve the others or create an unbounded transaction.
    """
    now = now or utcnow()
    batch_size = batch_size or settings.maintenance_batch_size
    job_id = job_id or uuid7()
    result = RetentionResult()

    record_ids = await _ids(
        db, select(IdempotencyRecord.id).where(IdempotencyRecord.expires_at < now)
        .order_by(IdempotencyRecord.expires_at, IdempotencyRecord.id), batch_size,
    )
    if record_ids:
        result.idempotency_records = (await db.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.id.in_(record_ids))
        )).rowcount or 0

    completed_cutoff = now - timedelta(days=settings.outbox_completed_retention_days)
    completed_ids = await _ids(
        db, select(OutboxCommand.id).where(
            OutboxCommand.status == "completed", OutboxCommand.completed_at < completed_cutoff,
        ).order_by(OutboxCommand.completed_at, OutboxCommand.id), batch_size,
    )
    if completed_ids:
        result.completed_outbox_commands = (await db.execute(
            delete(OutboxCommand).where(OutboxCommand.id.in_(completed_ids))
        )).rowcount or 0

    dead_cutoff = now - timedelta(days=settings.outbox_dead_retention_days)
    dead_ids = await _ids(
        db, select(OutboxCommand.id).where(
            OutboxCommand.status == "dead", OutboxCommand.updated_at < dead_cutoff,
        ).order_by(OutboxCommand.updated_at, OutboxCommand.id), batch_size,
    )
    if dead_ids:
        result.dead_outbox_commands = (await db.execute(
            delete(OutboxCommand).where(OutboxCommand.id.in_(dead_ids))
        )).rowcount or 0

    checkpoint_cutoff = now - timedelta(days=settings.checkpoint_retention_days)
    checkpoint_ids = await _ids(
        db, select(RunnerCheckpoint.id).join(
            WorkItem, WorkItem.id == RunnerCheckpoint.work_item_id,
        ).where(
            WorkItem.status.in_(TERMINAL_WORK_STATUSES),
            WorkItem.updated_at < checkpoint_cutoff,
            RunnerCheckpoint.created_at < checkpoint_cutoff,
        ).order_by(RunnerCheckpoint.created_at, RunnerCheckpoint.id), batch_size,
    )
    if checkpoint_ids:
        result.runner_checkpoints = (await db.execute(
            delete(RunnerCheckpoint).where(RunnerCheckpoint.id.in_(checkpoint_ids))
        )).rowcount or 0

    trace_cutoff = now - timedelta(days=settings.trace_retention_days)
    trace_ids = await _ids(
        db, select(RunnerTrace.id).where(
            RunnerTrace.status != "running", RunnerTrace.ended_at < trace_cutoff,
        ).order_by(RunnerTrace.ended_at, RunnerTrace.id), batch_size,
    )
    if trace_ids:
        span_ids = list(await db.scalars(select(ObservabilitySpan.id).where(
            ObservabilitySpan.runner_trace_id.in_(trace_ids),
        )))
        if span_ids:
            result.observability_span_links = (await db.execute(
                delete(ObservabilitySpanLink).where(ObservabilitySpanLink.span_id.in_(span_ids))
            )).rowcount or 0
            result.observability_spans = (await db.execute(
                delete(ObservabilitySpan).where(ObservabilitySpan.id.in_(span_ids))
            )).rowcount or 0
        result.runner_traces = (await db.execute(
            delete(RunnerTrace).where(RunnerTrace.id.in_(trace_ids))
        )).rowcount or 0

    delivery_cutoff = now - timedelta(days=settings.delivery_attempt_retention_days)
    attempt_ids = await _ids(
        db, select(NotificationDeliveryAttempt.id).where(
            NotificationDeliveryAttempt.status.in_(TERMINAL_DELIVERY_ATTEMPT_STATUSES),
            NotificationDeliveryAttempt.updated_at < delivery_cutoff,
        ).order_by(NotificationDeliveryAttempt.updated_at, NotificationDeliveryAttempt.id), batch_size,
    )
    if attempt_ids:
        result.notification_delivery_attempts = (await db.execute(
            delete(NotificationDeliveryAttempt).where(NotificationDeliveryAttempt.id.in_(attempt_ids))
        )).rowcount or 0

    counts = asdict(result)
    db.add(PlatformAuditEvent(
        id=uuid7(), occurred_at=now, category="maintenance", action="retention.cleanup",
        actor_type="service", actor_id="maintenance-worker", resource_type="retention_job",
        resource_id=job_id, outcome="succeeded", severity="info",
        summary="Bounded operational retention cleanup completed",
        changes={"deleted_counts": counts}, event_metadata={"batch_size": batch_size},
        dedupe_key=f"retention_cleanup:{job_id}",
    ))
    await db.flush()
    return result
