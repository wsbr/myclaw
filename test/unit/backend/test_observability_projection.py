from __future__ import annotations

import asyncio
import importlib
import secrets
from datetime import datetime, timedelta

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.telemetry import configure_telemetry, shutdown_telemetry
from app.database import Base
from app.models.common import uuid7
from app.models.observability_v1 import ObservabilitySpan, ObservabilitySpanLink, RunnerTrace
from app.models.runtime_v1 import DurableRunner
from app.services.observability_service import _projection_key, ensure_runner_trace, record_runner_span


pytestmark = pytest.mark.unit


@pytest.fixture
async def projection_store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'spans.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 9, 22, 3, 0, 0)
    trace = RunnerTrace(
        id=uuid7(), trace_id=secrets.token_hex(16), root_span_id=secrets.token_hex(8),
        runner_id=uuid7(), session_id=uuid7(), run_id=uuid7(),
        work_item_id=uuid7(), agent_id=uuid7(), status="running", started_at=now,
    )
    async with factory.begin() as db:
        db.add(trace)
    exporter = InMemorySpanExporter()
    configure_telemetry(exporter=exporter, force=True)
    try:
        yield engine, factory, trace, now, exporter
    finally:
        shutdown_telemetry()
        await engine.dispose()


@pytest.mark.asyncio
async def test_projection_replay_preserves_versions_and_regular_spans(projection_store):
    _, factory, trace, now, exporter = projection_store
    for fingerprint in ("a" * 64, "a" * 64, "b" * 64):
        await record_runner_span(
            trace.runner_id, name="agent.turn", span_kind="agent_turn",
            started_at=now, ended_at=now + timedelta(seconds=1),
            attributes={"fact_fingerprint": fingerprint},
            fact_fingerprint=fingerprint, session_factory=factory,
        )
    for _ in range(2):
        await record_runner_span(
            trace.runner_id, name="runner_execution", span_kind="runtime",
            started_at=now, ended_at=now + timedelta(seconds=1),
            session_factory=factory,
        )
    async with factory() as db:
        spans = list(await db.scalars(select(ObservabilitySpan)))
    assert len(spans) == 4
    assert len({s.projection_key for s in spans if s.projection_key}) == 2
    assert sum(s.projection_key is None for s in spans) == 2
    assert len(exporter.get_finished_spans()) == 4
    assert {s.span_id for s in spans} == {
        f"{s.context.span_id:016x}" for s in exporter.get_finished_spans()
    }


@pytest.mark.asyncio
async def test_projection_does_not_suppress_unrelated_integrity_error(projection_store, caplog):
    _, factory, trace, now, exporter = projection_store
    await record_runner_span(
        trace.runner_id, name="agent.turn", span_kind=None,
        started_at=now, ended_at=now, fact_fingerprint="a" * 64,
        session_factory=factory,
    )
    assert "Failed to record durable Runner span" in caplog.text
    assert not exporter.get_finished_spans()
    async with factory() as db:
        assert list(await db.scalars(select(ObservabilitySpan))) == []


@pytest.mark.asyncio
async def test_concurrent_trace_creation_reuses_the_winner(projection_store, caplog):
    engine, factory, trace, now, _ = projection_store
    runner = DurableRunner(
        id=uuid7(), work_item_id=uuid7(), run_id=trace.run_id,
        session_id=trace.session_id, agent_id=trace.agent_id,
        agent_version_id=uuid7(), execution_epoch=1, status="running",
    )
    async with factory.begin() as db:
        db.add(runner)
    barrier = asyncio.Barrier(4)

    class RacingSession(AsyncSession):
        async def flush(self, objects=None):
            if any(isinstance(row, RunnerTrace) for row in self.new):
                await asyncio.wait_for(barrier.wait(), timeout=10)
            await super().flush(objects)

    concurrent_factory = async_sessionmaker(engine, class_=RacingSession, expire_on_commit=False)
    traces = await asyncio.gather(*[
        ensure_runner_trace(runner.id, session_factory=concurrent_factory)
        for _ in range(4)
    ])
    assert all(item is not None for item in traces)
    assert len({item.id for item in traces}) == 1
    assert "Failed to create durable Runner trace" not in caplog.text


@pytest.mark.asyncio
async def test_backfill_keeps_legacy_duplicates_and_links_and_blocks_replay(projection_store):
    engine, factory, trace, now, exporter = projection_store
    spans = [ObservabilitySpan(
        id=uuid7(), runner_trace_id=trace.id, span_id=secrets.token_hex(8),
        name="agent.turn", span_kind="agent_turn", status="ok", started_at=now,
        ended_at=now, duration_ms=0, attributes={"fact_fingerprint": fingerprint},
    ) for fingerprint in ("a" * 64, "a" * 64, "b" * 64)]
    links = [ObservabilitySpanLink(
        id=uuid7(), span_id=span.id, linked_trace_id=secrets.token_hex(16),
        linked_span_id=secrets.token_hex(8), link_type="agent_invocation",
    ) for span in spans]
    async with factory.begin() as db:
        db.add_all(spans + links)
    migration = importlib.import_module("migrations.versions.0051_observability_projection_key")
    async with engine.begin() as connection:
        await connection.run_sync(lambda db: migration._backfill_projection_keys(db, batch_size=1))
        await connection.run_sync(lambda db: migration._backfill_projection_keys(db, batch_size=1))
    await record_runner_span(
        trace.runner_id, name="agent.turn", span_kind="agent_turn", started_at=now,
        ended_at=now, attributes={"fact_fingerprint": "a" * 64},
        fact_fingerprint="a" * 64, session_factory=factory,
    )
    async with factory() as db:
        saved = list(await db.scalars(select(ObservabilitySpan)))
        saved_links = list(await db.scalars(select(ObservabilitySpanLink)))
    assert {s.id for s in saved} == {s.id for s in spans}
    assert {(link.id, link.span_id) for link in saved_links} == {(link.id, link.span_id) for link in links}
    assert {s.projection_key for s in saved if s.projection_key} == {
        _projection_key(trace.id, "agent.turn", fingerprint)
        for fingerprint in ("a" * 64, "b" * 64)
    }
    assert sum(s.projection_key is None for s in saved) == 1
    assert not exporter.get_finished_spans()
