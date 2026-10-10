"""Exercise the actual checkpoint trigger and per-category commits in MySQL."""

from datetime import timedelta, timezone
import os

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord, PlatformAuditEvent
from app.models.runtime_v1 import Run, RunnerCheckpoint, WorkItem
from app.services import retention_service


pytestmark = pytest.mark.integration


def _engine():
    url = os.getenv("MYCLAW_V3_ACCEPTANCE_DATABASE_URL")
    if not url:
        pytest.skip("Select an explicitly isolated migrated MySQL test database")
    parsed = make_url(url)
    if not parsed.database or not parsed.database.endswith("_test"):
        raise ValueError("retention integration tests require a disposable *_test database")
    return create_async_engine(url, isolation_level="READ COMMITTED")


async def _seed(engine, *, checkpoint_old=True, work_terminal=True, run_terminal=True,
                recently_completed=False):
    now = utcnow()
    old = now - timedelta(days=31)
    run_id, work_id, checkpoint_id = uuid7(), uuid7(), uuid7()
    async with engine.connect() as connection:
        # The test database is disposable. Only fixture inserts bypass unrelated
        # foreign keys; the production trigger/constraints remain installed.
        await connection.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        try:
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                db.add_all([
                    Run(id=run_id, tenant_id=uuid7(), account_id=uuid7(),
                        session_id=uuid7(), user_id=uuid7(), mode="chat",
                        status="completed" if run_terminal else "active",
                        completed_at=(now if recently_completed else old) if run_terminal else None,
                        created_at=old, updated_at=old),
                    WorkItem(id=work_id, tenant_id=uuid7(), run_id=run_id,
                             session_id=uuid7(), user_id=uuid7(), mode="chat",
                             owner_agent_id=uuid7(), agent_version_id=uuid7(), goal="fixture",
                             status="succeeded" if work_terminal else "queued",
                             execution_epoch=1, attempt_no=1, created_at=old, updated_at=old),
                    RunnerCheckpoint(id=checkpoint_id, runner_id=uuid7(), work_item_id=work_id,
                                     sequence_no=1, checkpoint_type="settled", state_snapshot={},
                                     safe_to_resume=False, created_at=old if checkpoint_old else now),
                ])
                await db.flush()
            await connection.commit()
        finally:
            await connection.rollback()
            await connection.execute(text("SET FOREIGN_KEY_CHECKS=1"))
            await connection.commit()
    return checkpoint_id


@pytest.mark.asyncio
async def test_checkpoint_retention_requires_audited_expired_terminal_batch():
    engine = _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    expired = await _seed(engine)
    protected = [
        await _seed(engine, checkpoint_old=False),
        await _seed(engine, work_terminal=False),
        await _seed(engine, run_terminal=False),
        await _seed(engine, recently_completed=True),
    ]
    for statement in (
        "DELETE FROM runner_checkpoint WHERE id=:id",
        "UPDATE runner_checkpoint SET state_snapshot=JSON_OBJECT('changed',true) WHERE id=:id",
    ):
        async with factory() as db:
            with pytest.raises(DBAPIError, match="append-only"):
                await db.execute(text(statement), {"id": expired})
            await db.rollback()

    # Merely owning the leader lock is not an allowlist. Even an explicit audit
    # cannot bypass age, active work, active run or recently completed parents.
    async with engine.connect() as connection:
        await connection.execute(text("SELECT GET_LOCK('myclaw.retention_cleanup',0)"))
        await connection.commit()
        try:
            async with AsyncSession(bind=connection) as db:
                with pytest.raises(DBAPIError, match="append-only"):
                    await db.execute(text("DELETE FROM runner_checkpoint WHERE id=:id"), {"id": expired})
                await db.rollback()
                for checkpoint_id in protected:
                    db.add(PlatformAuditEvent(
                        id=uuid7(), occurred_at=utcnow(), category="maintenance",
                        action="retention.checkpoint_batch", actor_type="service",
                        actor_id="maintenance-worker", resource_type="retention_job",
                        resource_id=uuid7(), outcome="succeeded", severity="info",
                        summary="Integration guard test", changes={"checkpoint_ids": [checkpoint_id]},
                        event_metadata={
                            "connection_id": await db.scalar(text("SELECT CONNECTION_ID()")),
                            "cutoff_at": (utcnow() - timedelta(days=30)).isoformat(sep=" "),
                        }, dedupe_key=f"retention-guard:{uuid7()}",
                    ))
                    await db.flush()
                    with pytest.raises(DBAPIError, match="append-only"):
                        await db.execute(text("DELETE FROM runner_checkpoint WHERE id=:id"), {"id": checkpoint_id})
                    await db.rollback()
        finally:
            await connection.execute(text("SELECT RELEASE_LOCK('myclaw.retention_cleanup')"))
            await connection.commit()

    job_id = uuid7()
    async with factory() as db:
        result = await retention_service.run_retention_cleanup(db, job_id=job_id)
    assert not result.failures
    assert result.runner_checkpoints >= 1
    async with factory() as db:
        assert await db.get(RunnerCheckpoint, expired) is None
        for checkpoint_id in protected:
            assert await db.get(RunnerCheckpoint, checkpoint_id) is not None
        audit = await db.scalar(select(PlatformAuditEvent).where(
            PlatformAuditEvent.dedupe_key == f"retention_checkpoint_batch:{job_id}",
        ))
        assert expired in audit.changes["checkpoint_ids"]
        assert not (set(protected) & set(audit.changes["checkpoint_ids"]))
        audit_id = audit.id
    for statement in (
        "DELETE FROM platform_audit_event WHERE id=:id",
        "UPDATE platform_audit_event SET changes=JSON_OBJECT() WHERE id=:id",
    ):
        async with factory() as db:
            with pytest.raises(DBAPIError, match="retention batch audit is append-only"):
                await db.execute(text(statement), {"id": audit_id})
            await db.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_mysql_trigger_failure_does_not_undo_other_retention_categories(monkeypatch):
    engine = _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    protected = await _seed(engine, checkpoint_old=False)
    idem_id, job_id = uuid7(), uuid7()
    async with factory.begin() as db:
        db.add(IdempotencyRecord(
            id=idem_id, principal_type="account", principal_id=uuid7(), operation="test",
            idempotency_key=uuid7(), request_hash="a" * 64,
            expires_at=utcnow() - timedelta(days=1),
        ))

    async def blocked(db, now, batch_size, job_id, result):
        await db.execute(delete(RunnerCheckpoint).where(RunnerCheckpoint.id == protected))

    monkeypatch.setattr(retention_service, "_CATEGORIES", (
        ("checkpoints", blocked), ("idempotency", retention_service._cleanup_idempotency),
    ))
    async with factory() as db:
        result = await retention_service.run_retention_cleanup(db, job_id=job_id)
    assert "checkpoints" in result.failures
    assert result.idempotency_records >= 1
    async with factory() as db:
        assert await db.get(RunnerCheckpoint, protected) is not None
        assert await db.get(IdempotencyRecord, idem_id) is None
        audit = await db.scalar(select(PlatformAuditEvent).where(
            PlatformAuditEvent.dedupe_key == f"retention_cleanup:{job_id}",
        ))
        assert audit.outcome == "failed"
        assert audit.changes["deleted_counts"]["idempotency_records"] >= 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_retention_preserves_microsecond_cutoff_at_thirty_day_boundary():
    engine = _engine()
    checkpoint_id = await _seed(engine)
    fixed_db_time = utcnow().replace(microsecond=900000)
    async with engine.connect() as connection:
        await connection.execute(text("SET timestamp=:epoch"), {
            "epoch": fixed_db_time.replace(tzinfo=timezone.utc).timestamp(),
        })
        await connection.commit()
        try:
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                # DATETIME without (6) rounds .8 to the next second and would
                # falsely reject this cutoff relative to the server's .9.
                result = await retention_service.run_retention_cleanup(
                    db, now=fixed_db_time.replace(microsecond=800000),
                )
                assert not result.failures
                assert await db.get(RunnerCheckpoint, checkpoint_id) is None
        finally:
            await connection.rollback()
            await connection.execute(text("SET timestamp=0"))
            await connection.commit()
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_owns_lock", [False, True])
async def test_failed_audit_flush_does_not_leak_or_reenter_leader_lock(caller_owns_lock):
    engine = _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    checkpoint_id, job_id = await _seed(engine), uuid7()
    async with factory.begin() as db:
        # Force the real ORM flush into its failed-transaction state.
        db.add(PlatformAuditEvent(
            id=uuid7(), occurred_at=utcnow(), category="maintenance",
            action="retention.checkpoint_batch", actor_type="service",
            actor_id="maintenance-worker", resource_type="retention_job",
            resource_id=job_id, outcome="succeeded", severity="info",
            summary="Collision fixture", changes={"checkpoint_ids": []},
            event_metadata={}, dedupe_key=f"retention_checkpoint_batch:{job_id}",
        ))
    async with engine.connect() as connection:
        if caller_owns_lock:
            assert await connection.scalar(text("SELECT GET_LOCK('myclaw.retention_cleanup',0)")) == 1
            await connection.commit()
        async with AsyncSession(bind=connection, expire_on_commit=False) as db:
            result = await retention_service.run_retention_cleanup(db, job_id=job_id)
            assert result.failures == {"checkpoints": "IntegrityError"}
            assert await db.get(RunnerCheckpoint, checkpoint_id) is not None
        if caller_owns_lock:
            assert await connection.scalar(text("SELECT RELEASE_LOCK('myclaw.retention_cleanup')")) == 1
        assert await connection.scalar(text("SELECT IS_USED_LOCK('myclaw.retention_cleanup')")) is None
        await connection.commit()
    # A separate session can acquire it afterwards, even when the first holder
    # is retained in the pool rather than being physically disconnected.
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT GET_LOCK('myclaw.retention_cleanup',0)")) == 1
        assert await connection.scalar(text("SELECT RELEASE_LOCK('myclaw.retention_cleanup')")) == 1
        await connection.commit()
    await engine.dispose()
