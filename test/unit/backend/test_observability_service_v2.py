"""Durable, redacted Runner Trace projection tests."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.common import utcnow, uuid7
from app.models.observability_v1 import (
    ObservabilitySpan,
    ObservabilitySpanLink,
    RunnerTrace,
)
from app.models.runtime_v1 import (
    AgentInvocation,
    AgentTurn,
    DurableRunner,
    ProviderAttempt,
    WorkItem,
)
from app.services.observability_service import (
    ensure_runner_trace,
    finish_runner_trace,
    record_runner_fact_spans,
    record_runner_span,
)
from unit.backend.runtime_worker_testkit import create_runtime_world


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_committed_turns_project_to_redacted_token_spans():
    world = await create_runtime_world()
    turn_id = uuid7()
    async with world.factory.begin() as db:
        runner = await db.get(DurableRunner, world.runner_id)
        runner.status = "succeeded"
        db.add(AgentTurn(
            id=turn_id,
            runner_id=runner.id,
            work_item_id=runner.work_item_id,
            run_id=runner.run_id,
            agent_version_id=runner.agent_version_id,
            capability_snapshot_id=uuid7(),
            turn_no=1,
            status="completed",
            context_manifest={},
            input_hash="a" * 64,
            turn_items=[],
            stop_reason="end_turn",
            usage={"input_tokens": 17, "output_tokens": 5},
            started_at=utcnow(),
            ended_at=utcnow(),
        ))
        db.add(ProviderAttempt(
            agent_turn_id=turn_id,
            model_version_id=uuid7(),
            attempt_no=1,
            provider="openai_compatible",
            status="succeeded",
            request_hash="b" * 64,
            received_payload=True,
            usage={"input_tokens": 17, "output_tokens": 5},
            started_at=utcnow(),
            ended_at=utcnow(),
        ))

    assert await ensure_runner_trace(
        world.runner_id, session_factory=world.factory,
    ) is not None
    await record_runner_fact_spans(
        world.runner_id, session_factory=world.factory,
    )
    # Reprojection of the same committed ledger snapshot is idempotent.
    await record_runner_fact_spans(
        world.runner_id, session_factory=world.factory,
    )
    now = utcnow()
    await record_runner_span(
        world.runner_id,
        name="runner_execution",
        span_kind="runner",
        started_at=now,
        ended_at=now,
        session_factory=world.factory,
    )
    await finish_runner_trace(
        world.runner_id, session_factory=world.factory,
    )

    async with world.factory() as db:
        trace = await db.scalar(select(RunnerTrace).where(
            RunnerTrace.runner_id == world.runner_id,
        ))
        spans = list(await db.scalars(select(ObservabilitySpan).where(
            ObservabilitySpan.runner_trace_id == trace.id,
        ).order_by(ObservabilitySpan.name)))
        assert trace.status == "completed" and trace.ended_at is not None
        assert [span.name for span in spans] == [
            "agent.turn", "model_generation", "provider.attempt", "runner_execution",
        ]
        token_spans = [span for span in spans if span.input_tokens is not None]
        assert all(
            (span.input_tokens, span.output_tokens) == (17, 5)
            for span in token_spans
        )
        assert all("prompt" not in str(span.attributes).lower() for span in spans)
    await world.engine.dispose()


@pytest.mark.asyncio
async def test_agent_invocation_creates_cross_runner_span_link():
    world = await create_runtime_world()
    target_work_id, target_runner_id, invocation_id = uuid7(), uuid7(), uuid7()
    await ensure_runner_trace(world.runner_id, session_factory=world.factory)
    async with world.factory.begin() as db:
        caller = await db.get(DurableRunner, world.runner_id)
        target_work = WorkItem(
            id=target_work_id,
            run_id=caller.run_id,
            session_id=caller.session_id,
            user_id="u1",
            mode="chat",
            owner_agent_id=caller.agent_id,
            agent_version_id=caller.agent_version_id,
            parent_work_item_id=caller.work_item_id,
            goal="review",
            purpose="delegation",
            status="running",
            active_runner_id=target_runner_id,
            execution_epoch=1,
            attempt_no=1,
        )
        target_runner = DurableRunner(
            id=target_runner_id,
            work_item_id=target_work_id,
            run_id=caller.run_id,
            session_id=caller.session_id,
            agent_id=caller.agent_id,
            agent_version_id=caller.agent_version_id,
            execution_epoch=1,
            status="running",
            principal_snapshot={},
            assembly_snapshot={},
            prompt_manifest={},
            input_snapshot={},
        )
        db.add_all([target_work, target_runner, AgentInvocation(
            id=invocation_id,
            run_id=caller.run_id,
            caller_work_item_id=caller.work_item_id,
            caller_runner_id=caller.id,
            caller_agent_id=caller.agent_id,
            target_work_item_id=target_work_id,
            target_agent_id=caller.agent_id,
            target_agent_version_id=caller.agent_version_id,
            mode="chat",
            return_route="caller",
            request_text="review",
            context_snapshot={},
            status="running",
            idempotency_key="invoke-1",
            operation_key="invoke-1",
            request_hash="c" * 64,
            authorization_snapshot={},
        )])

    target_trace = await ensure_runner_trace(
        target_runner_id, session_factory=world.factory,
    )
    async with world.factory() as db:
        span = await db.scalar(select(ObservabilitySpan).where(
            ObservabilitySpan.runner_trace_id == target_trace.id,
            ObservabilitySpan.name == "invoke_agent.link",
        ))
        link = await db.scalar(select(ObservabilitySpanLink).where(
            ObservabilitySpanLink.span_id == span.id,
        ))
        caller_trace = await db.scalar(select(RunnerTrace).where(
            RunnerTrace.runner_id == world.runner_id,
        ))
        assert link.link_type == "agent_invocation"
        assert link.linked_trace_id == caller_trace.trace_id
        assert link.entity_id == invocation_id
    await world.engine.dispose()
