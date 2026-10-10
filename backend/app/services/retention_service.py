"""Bounded retention cleanup for disposable operational records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.config import settings
from app.models.channel_v1 import NotificationDeliveryAttempt
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord, OutboxCommand, PlatformAuditEvent
from app.models.observability_v1 import ObservabilitySpan, ObservabilitySpanLink, RunnerTrace
from app.models.runtime_v1 import Run, RunnerCheckpoint, WorkerInstance, WorkItem


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
    worker_instances: int = 0
    failures: dict[str, str] = field(default_factory=dict)


async def _ids(db: AsyncSession, statement, limit: int) -> list[str]:
    return list(await db.scalars(statement.limit(limit)))


async def _cleanup_idempotency(db, now, batch_size, job_id, result) -> None:
    record_ids = await _ids(
        db, select(IdempotencyRecord.id).where(IdempotencyRecord.expires_at < now)
        .order_by(IdempotencyRecord.expires_at, IdempotencyRecord.id), batch_size,
    )
    if record_ids:
        result.idempotency_records = (await db.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.id.in_(record_ids))
        )).rowcount or 0


async def _cleanup_completed_outbox(db, now, batch_size, job_id, result) -> None:
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


async def _cleanup_dead_outbox(db, now, batch_size, job_id, result) -> None:
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


async def _cleanup_checkpoints(db, now, batch_size, job_id, result) -> None:
    checkpoint_cutoff = now - timedelta(days=settings.checkpoint_retention_days)
    checkpoint_ids = await _ids(
        db, select(RunnerCheckpoint.id).join(
            WorkItem, WorkItem.id == RunnerCheckpoint.work_item_id,
        ).join(Run, Run.id == WorkItem.run_id).where(
            WorkItem.status.in_(TERMINAL_WORK_STATUSES),
            Run.status.in_(("completed", "failed", "cancelled")),
            Run.completed_at < checkpoint_cutoff,
            WorkItem.updated_at < checkpoint_cutoff,
            RunnerCheckpoint.created_at < checkpoint_cutoff,
        ).order_by(RunnerCheckpoint.created_at, RunnerCheckpoint.id), batch_size,
    )
    if not checkpoint_ids:
        return
    connection_id = (
        await db.scalar(text("SELECT CONNECTION_ID()"))
        if db.bind.dialect.name == "mysql" else None
    )
    # The protected allowlist and deletion share one transaction. The trigger
    # checks the connection, leader lock, age, IDs and terminal parents.
    db.add(PlatformAuditEvent(
        id=uuid7(), occurred_at=utcnow(), category="maintenance",
        action="retention.checkpoint_batch", actor_type="service",
        actor_id="maintenance-worker", resource_type="retention_job",
        resource_id=job_id, outcome="succeeded", severity="info",
        summary="Expired terminal checkpoints selected for retention",
        changes={"checkpoint_ids": checkpoint_ids},
        event_metadata={
            "connection_id": connection_id,
            "cutoff_at": checkpoint_cutoff.isoformat(sep=" "),
        },
        dedupe_key=f"retention_checkpoint_batch:{job_id}",
    ))
    await db.flush()
    result.runner_checkpoints = (await db.execute(
        delete(RunnerCheckpoint).where(RunnerCheckpoint.id.in_(checkpoint_ids))
    )).rowcount or 0


async def _cleanup_traces(db, now, batch_size, job_id, result) -> None:
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


async def _cleanup_delivery_attempts(db, now, batch_size, job_id, result) -> None:
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


async def _cleanup_workers(db, now, batch_size, job_id, result) -> None:
    worker_cutoff = now - timedelta(days=settings.worker_heartbeat_retention_days)
    worker_ids = await _ids(
        db, select(WorkerInstance.worker_id).where(
            WorkerInstance.heartbeat_at < worker_cutoff,
        ).order_by(WorkerInstance.heartbeat_at, WorkerInstance.worker_id), batch_size,
    )
    if worker_ids:
        result.worker_instances = (await db.execute(
            delete(WorkerInstance).where(WorkerInstance.worker_id.in_(worker_ids))
        )).rowcount or 0


_CATEGORIES = (
    ("idempotency", _cleanup_idempotency),
    ("completed_outbox", _cleanup_completed_outbox),
    ("dead_outbox", _cleanup_dead_outbox),
    ("checkpoints", _cleanup_checkpoints),
    ("traces", _cleanup_traces),
    ("delivery_attempts", _cleanup_delivery_attempts),
    ("workers", _cleanup_workers),
)


def _counts(result: RetentionResult) -> dict[str, int]:
    values = asdict(result)
    values.pop("failures")
    return values


def _audit(*, job_id, category, now, batch_size, counts, failure=None):
    return PlatformAuditEvent(
        id=uuid7(), occurred_at=now, category="maintenance",
        action="retention.cleanup" if category is None else "retention.category",
        actor_type="service", actor_id="maintenance-worker", resource_type="retention_job",
        resource_id=job_id, outcome="failed" if failure else "succeeded",
        severity="warning" if failure else "info",
        summary="Operational retention cleanup failed" if failure else
                "Bounded operational retention cleanup completed",
        changes={"deleted_counts": counts},
        event_metadata={"batch_size": batch_size, "category": category, "failure": failure},
        dedupe_key=f"retention_cleanup:{job_id}" if category is None else
                   f"retention_category:{job_id}:{category}",
    )


async def _run_categories(
    db: AsyncSession, *, now: datetime | None = None, batch_size: int | None = None,
    job_id: str | None = None,
) -> RetentionResult:
    """Own one bounded transaction per category and persist partial outcomes.

    Pass an idle session, not a caller-owned transaction. A category failure
    rolls back its audit and deletes, without undoing other categories. A lost
    database connection can still abort the job; prior committed work remains.
    Domain facts (Session/Run/WorkItem/Event/Artifact) are never purged here.
    """
    if db.in_transaction():
        raise ValueError("retention cleanup requires an idle session")
    now = now or utcnow()
    batch_size = settings.maintenance_batch_size if batch_size is None else batch_size
    if batch_size < 1 or batch_size > 10000:
        raise ValueError("retention batch_size must be between 1 and 10000")
    job_id = job_id or uuid7()
    result = RetentionResult()
    for category, cleanup in _CATEGORIES:
        partial = RetentionResult()
        try:
            async with db.begin():
                await cleanup(db, now, batch_size, job_id, partial)
                db.add(_audit(
                    job_id=job_id, category=category, now=now, batch_size=batch_size,
                    counts=_counts(partial),
                ))
            for name, count in _counts(partial).items():
                setattr(result, name, getattr(result, name) + count)
        except Exception as error:
            # Error classes are safe to expose; SQL text/parameters and raw
            # provider/driver details are deliberately excluded from audit.
            result.failures[category] = type(error).__name__
            async with db.begin():
                db.add(_audit(
                    job_id=job_id, category=category, now=now, batch_size=batch_size,
                    counts=_counts(RetentionResult()), failure=type(error).__name__,
                ))
    async with db.begin():
        db.add(_audit(
            job_id=job_id, category=None, now=now, batch_size=batch_size,
            counts=_counts(result), failure=result.failures or None,
        ))
    return result


async def _run_on_connection(db: AsyncSession, connection: AsyncConnection, **kwargs):
    acquired = False
    mysql = connection.dialect.name == "mysql"
    try:
        if mysql:
            owner = await connection.scalar(text("SELECT IS_USED_LOCK('myclaw.retention_cleanup')"))
            connection_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
            # A Worker caller may already own this connection's leader lock.
            # Never increment MySQL's reentrant lock count in that case.
            if owner != connection_id:
                acquired = bool(await connection.scalar(text(
                    "SELECT GET_LOCK('myclaw.retention_cleanup', 0)"
                )))
                if not acquired:
                    await connection.commit()
                    return RetentionResult(failures={"leader_lock": "busy"})
            await connection.commit()
        return await _run_categories(db, **kwargs)
    finally:
        if acquired:
            # ORM flush errors leave the Session unusable until rollback. Lock
            # cleanup runs on the pinned Connection, after all transactions.
            await db.rollback()
            if not connection.invalidated:
                await connection.rollback()
                await connection.execute(text("SELECT RELEASE_LOCK('myclaw.retention_cleanup')"))
                await connection.commit()


async def run_retention_cleanup(
    db: AsyncSession, *, now: datetime | None = None, batch_size: int | None = None,
    job_id: str | None = None,
) -> RetentionResult:
    """Run independent category transactions on a pinned maintenance connection."""
    if db.in_transaction():
        raise ValueError("retention cleanup requires an idle session")
    kwargs = {"now": now, "batch_size": batch_size, "job_id": job_id}
    if isinstance(db.bind, AsyncEngine):
        async with db.bind.connect() as connection:
            async with AsyncSession(bind=connection, expire_on_commit=False) as bound:
                return await _run_on_connection(bound, connection, **kwargs)
    if isinstance(db.bind, AsyncConnection):
        if db.bind.in_transaction():
            raise ValueError("retention cleanup requires an idle connection")
        return await _run_on_connection(db, db.bind, **kwargs)
    raise ValueError("retention cleanup requires an async database bind")
