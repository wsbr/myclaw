"""Real concurrent MySQL transactions for non-authoritative fact projections."""

from __future__ import annotations

import asyncio
import importlib
import os
import secrets
from datetime import timedelta

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.telemetry import configure_telemetry, shutdown_telemetry
from app.models.common import utcnow, uuid7
from app.models.observability_v1 import ObservabilitySpan, ObservabilitySpanLink, RunnerTrace
from app.models.runtime_v1 import AgentTurn
from app.services.observability_service import (
    _projection_key, record_runner_fact_spans, record_runner_span,
)


pytestmark = pytest.mark.integration


@pytest.fixture
async def mysql_projection_store():
    url = os.getenv("MYCLAW_V3_ACCEPTANCE_DATABASE_URL")
    if not url:
        pytest.skip("Select an isolated migrated MySQL with MYCLAW_V3_ACCEPTANCE_DATABASE_URL")
    engine = create_async_engine(url, isolation_level="READ COMMITTED", pool_size=10)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = utcnow().replace(microsecond=0)
    trace = RunnerTrace(
        id=uuid7(), trace_id=secrets.token_hex(16), root_span_id=secrets.token_hex(8),
        runner_id=uuid7(), session_id=uuid7(), run_id=uuid7(),
        work_item_id=uuid7(), agent_id=uuid7(), status="running", started_at=now,
    )
    turn = AgentTurn(
        id=uuid7(), runner_id=trace.runner_id, work_item_id=trace.work_item_id,
        run_id=trace.run_id, agent_version_id=uuid7(), capability_snapshot_id=uuid7(),
        turn_no=1, status="started", input_hash="a" * 64, started_at=now,
        usage={"input_tokens": 3, "output_tokens": 2},
    )
    async with factory.begin() as db:
        # Only fixture creation bypasses unrelated graph FKs, following the
        # other disposable-MySQL schema tests. All tested writes retain checks.
        await db.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        try:
            db.add_all([trace, turn])
            await db.flush()
        finally:
            await db.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    exporter = InMemorySpanExporter()
    configure_telemetry(exporter=exporter, force=True)
    try:
        yield engine, factory, trace, turn, now, exporter
    finally:
        shutdown_telemetry()
        await engine.dispose()


@pytest.mark.asyncio
async def test_mysql_concurrent_fact_projection_reserves_once_before_export(mysql_projection_store):
    engine, factory, trace, turn, now, exporter = mysql_projection_store
    barrier = asyncio.Barrier(8)

    class RacingSession(AsyncSession):
        async def flush(self, objects=None):
            if any(isinstance(row, ObservabilitySpan) and row.projection_key for row in self.new):
                # All eight independent transactions reach the actual INSERT
                # before any may proceed. Persistence and exporter are real.
                await asyncio.wait_for(barrier.wait(), timeout=15)
            await super().flush(objects)

    concurrent_factory = async_sessionmaker(engine, class_=RacingSession, expire_on_commit=False)
    await asyncio.gather(*[
        record_runner_fact_spans(trace.runner_id, session_factory=concurrent_factory)
        for _ in range(8)
    ])
    async with factory.begin() as db:
        saved_turn = await db.get(AgentTurn, turn.id)
        saved_turn.status = "completed"
        saved_turn.ended_at = now + timedelta(seconds=1)
    # A genuinely different committed fact version must remain a new span.
    await asyncio.gather(*[
        record_runner_fact_spans(trace.runner_id, session_factory=concurrent_factory)
        for _ in range(8)
    ])
    await asyncio.gather(*[
        record_runner_span(
            trace.runner_id, name="runner_execution", span_kind="runtime",
            started_at=now, ended_at=now, session_factory=factory,
        ) for _ in range(8)
    ])
    async with factory() as db:
        rows = list(await db.scalars(select(ObservabilitySpan).where(
            ObservabilitySpan.runner_trace_id == trace.id,
        )))
    projected = [row for row in rows if row.projection_key]
    assert len(projected) == 2
    assert {tuple(row.attributes["statuses"]) for row in projected} == {("started",), ("completed",)}
    assert len(rows) == 10
    assert len(exporter.get_finished_spans()) == 10
    assert {row.span_id for row in rows} == {
        f"{span.context.span_id:016x}" for span in exporter.get_finished_spans()
    }


@pytest.mark.asyncio
async def test_mysql_projection_backfill_preserves_historical_spans_and_links(mysql_projection_store):
    engine, factory, trace, _, now, exporter = mysql_projection_store
    originals = [ObservabilitySpan(
        id=uuid7(), runner_trace_id=trace.id, span_id=secrets.token_hex(8),
        name="agent.turn", span_kind="agent_turn", status="ok", started_at=now,
        ended_at=now, duration_ms=0, attributes={"fact_fingerprint": "a" * 64},
    ) for _ in range(2)]
    links = [ObservabilitySpanLink(
        id=uuid7(), span_id=span.id, linked_trace_id=secrets.token_hex(16),
        linked_span_id=secrets.token_hex(8), link_type="agent_invocation",
    ) for span in originals]
    async with factory.begin() as db:
        db.add_all(originals)
        await db.flush()
        db.add_all(links)
    migration = importlib.import_module("migrations.versions.0051_observability_projection_key")
    async with engine.begin() as connection:
        await connection.run_sync(lambda db: migration._backfill_projection_keys(db, batch_size=1))
    await record_runner_span(
        trace.runner_id, name="agent.turn", span_kind="agent_turn", started_at=now,
        ended_at=now, fact_fingerprint="a" * 64, session_factory=factory,
    )
    async with factory() as db:
        rows = list(await db.scalars(select(ObservabilitySpan).where(
            ObservabilitySpan.runner_trace_id == trace.id,
        )))
        saved_links = list(await db.scalars(select(ObservabilitySpanLink).where(
            ObservabilitySpanLink.span_id.in_([row.id for row in originals]),
        )))
    assert {row.id for row in rows} == {row.id for row in originals}
    assert [row.projection_key for row in rows if row.projection_key] == [
        _projection_key(trace.id, "agent.turn", "a" * 64),
    ]
    assert {(row.id, row.span_id) for row in saved_links} == {(row.id, row.span_id) for row in links}
    assert not exporter.get_finished_spans()
