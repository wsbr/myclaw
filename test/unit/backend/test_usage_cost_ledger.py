from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import *  # noqa: F401,F403
from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, UserIdentity, UserSession
from app.models.registry import ModelVersion
from app.models.runtime_v1 import (
    AgentTurn, DurableRunner, ProviderAttempt, Run, ToolAttempt, ToolInvocation,
    WorkItem,
)
from app.models.usage_v1 import UsageLedgerEntry
from app.services.usage_ledger_service import (
    create_price_snapshot,
    create_usage_adjustment,
    record_provider_attempt_usage,
    record_tool_attempt_usage,
    usage_rollup,
)


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_provider_attempt_usage_is_append_only_priced_and_idempotent():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory.begin() as db:
        account_id, user_id, session_id = uuid7(), uuid7(), uuid7()
        run_id, work_id, runner_id, turn_id = uuid7(), uuid7(), uuid7(), uuid7()
        agent_version_id, model_version_id, attempt_id = uuid7(), uuid7(), uuid7()
        db.add(UserIdentity(user_id=user_id, status="active"))
        db.add(PlatformAccount(
            id=account_id, user_id=user_id, display_name="usage-owner",
            status="active", auth_revision=1,
        ))
        db.add(UserSession(
            id=session_id, user_id=user_id, account_id=account_id,
            entry_agent_id=uuid7(), status="active", origin_type="web",
            next_event_sequence=1, permission_mode="direct", permission_revision=1,
            interaction_mode="chat", interaction_revision=1,
        ))
        db.add(Run(
            id=run_id, session_id=session_id, user_id=user_id, mode="chat",
            status="active", permission_mode="direct", hard_budget_snapshot={},
        ))
        db.add(WorkItem(
            id=work_id, run_id=run_id, session_id=session_id, user_id=user_id,
            mode="chat", owner_agent_id=uuid7(), agent_version_id=agent_version_id,
            goal="usage ledger", purpose="root_request", status="running",
            execution_epoch=1, attempt_no=1,
        ))
        db.add(DurableRunner(
            id=runner_id, work_item_id=work_id, run_id=run_id,
            session_id=session_id, agent_id=uuid7(),
            agent_version_id=agent_version_id, execution_epoch=1,
            status="running", principal_snapshot={}, assembly_snapshot={},
            prompt_manifest={}, input_snapshot={},
        ))
        db.add(ModelVersion(
            id=model_version_id, model_id=uuid7(), version_no=1,
            provider="provider-a", model_name="model-a",
            default_parameters={}, capabilities={"usage_in_stream": True},
            adapter_protocol="agent_native.v2", content_hash="a" * 64,
            published_at=utcnow(),
        ))
        turn = AgentTurn(
            id=turn_id, runner_id=runner_id, work_item_id=work_id,
            run_id=run_id, agent_version_id=agent_version_id,
            capability_snapshot_id=uuid7(), turn_no=1, status="completed",
            context_manifest={}, input_hash="b" * 64, turn_items=[], usage={},
            started_at=utcnow(), ended_at=utcnow(),
        )
        now = utcnow()
        attempt = ProviderAttempt(
            id=attempt_id, agent_turn_id=turn_id,
            model_version_id=model_version_id, attempt_no=1,
            provider="provider-a", status="succeeded", request_hash="c" * 64,
            usage={
                "input_tokens": 100,
                "cached_input_tokens": 10,
                "output_tokens": 20,
            },
            started_at=now - timedelta(milliseconds=250), ended_at=now,
        )
        db.add_all([turn, attempt])
        await db.flush()
        await create_price_snapshot(
            db, subject_type="model", provider="provider-a",
            subject_key="model-a", metric="input_tokens", unit="token",
            currency="USD", price_per_unit=Decimal("0.000001"),
            price_status="contracted", effective_from=now - timedelta(days=1),
            effective_to=None, source_label="test contract",
            actor_account_id=account_id,
        )
        first = await record_provider_attempt_usage(
            db, attempt=attempt, turn=turn, runner_id=runner_id,
            work_item_id=work_id, run_id=run_id,
            agent_version_id=agent_version_id,
        )
        replay = await record_provider_attempt_usage(
            db, attempt=attempt, turn=turn, runner_id=runner_id,
            work_item_id=work_id, run_id=run_id,
            agent_version_id=agent_version_id,
        )
        assert len(first) == len(replay) == 5
        assert {item.id for item in first} == {item.id for item in replay}
        priced = next(item for item in first if item.metric == "input_tokens")
        assert priced.quantity == Decimal("100")
        assert priced.cost_status == "contracted"
        assert priced.cost_amount == Decimal("0.000100")
        assert priced.price_snapshot_id
        output = next(item for item in first if item.metric == "output_tokens")
        assert output.measurement_status == "measured"
        assert output.cost_status == "unknown"
        assert output.cost_amount is None
        adjustment = await create_usage_adjustment(
            db, original=priced, quantity_delta=Decimal("-20"),
            reason="provider reconciliation", operation_key="adjustment:test",
        )
        adjustment_replay = await create_usage_adjustment(
            db, original=priced, quantity_delta=Decimal("-20"),
            reason="provider reconciliation", operation_key="adjustment:test",
        )
        assert adjustment_replay.id == adjustment.id
        assert adjustment.adjusts_entry_id == priced.id
        assert adjustment.cost_amount == Decimal("-0.000020")
        rollup = usage_rollup([*first, adjustment], group_by="run_id")
        assert rollup[0]["key"] == run_id
        assert rollup[0]["costs"] == {"USD:contracted": "0.000080000000"}
        assert rollup[0]["unknown_cost_entries"] == 4
        rows = list(await db.scalars(select(UsageLedgerEntry)))
        assert len(rows) == 6

        await create_price_snapshot(
            db, subject_type="tool", provider=None,
            subject_key="execute", metric="tool_units", unit="invocation",
            currency="USD", price_per_unit=Decimal("0.01"),
            price_status="estimated", effective_from=now - timedelta(days=1),
            effective_to=None, source_label="tool estimate",
            actor_account_id=account_id,
        )
        tool_invocation = ToolInvocation(
            id=uuid7(), runner_id=runner_id, work_item_id=work_id,
            run_id=run_id, session_id=session_id, platform_tool_id=uuid7(),
            tool_version_id=uuid7(), tool_version_content_hash="d" * 64,
            tool_key="execute", source_turn_id=turn_id,
            operation_key="execute-once", authorization_snapshot={},
            execution_mode="durable_async", status="succeeded",
            arguments={}, arguments_hash="e" * 64,
            business_idempotency_key="execute-business",
            principal_hash="f" * 64, attempt_count=1, execution_epoch=1,
        )
        tool_attempt = ToolAttempt(
            id=uuid7(), tool_invocation_id=tool_invocation.id, attempt_no=1,
            status="succeeded", request_snapshot={}, retryable=False,
            execution_epoch=1, fencing_token=1,
            started_at=now - timedelta(milliseconds=500), ended_at=now,
        )
        db.add_all([tool_invocation, tool_attempt])
        await db.flush()
        tool_entries = await record_tool_attempt_usage(
            db, attempt=tool_attempt, invocation=tool_invocation,
        )
        tool_replay = await record_tool_attempt_usage(
            db, attempt=tool_attempt, invocation=tool_invocation,
        )
        assert len(tool_entries) == len(tool_replay) == 2
        assert {item.id for item in tool_entries} == {item.id for item in tool_replay}
        tool_unit = next(item for item in tool_entries if item.metric == "tool_units")
        assert tool_unit.cost_status == "estimated"
        assert tool_unit.cost_amount == Decimal("0.01")
        sandbox_time = next(
            item for item in tool_entries if item.metric == "sandbox_milliseconds"
        )
        assert sandbox_time.quantity == Decimal("500")
        assert sandbox_time.cost_status == "unknown"
    await engine.dispose()


@pytest.mark.asyncio
async def test_price_windows_cannot_overlap_for_the_same_metric():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory.begin() as db:
        account_id, user_id = uuid7(), uuid7()
        db.add(UserIdentity(user_id=user_id, status="active"))
        db.add(PlatformAccount(
            id=account_id, user_id=user_id, display_name="price-owner",
            status="active", auth_revision=1,
        ))
        await db.flush()
        now = utcnow()
        arguments = dict(
            subject_type="model", provider="provider-a", subject_key="model-a",
            metric="input_tokens", unit="token", currency="USD",
            price_per_unit=Decimal("0.000001"), price_status="contracted",
            source_label="contract", actor_account_id=account_id,
        )
        first = await create_price_snapshot(
            db, **arguments, effective_from=now, effective_to=None,
        )
        replay = await create_price_snapshot(
            db, **arguments, effective_from=now, effective_to=None,
        )
        assert replay.id == first.id
        with pytest.raises(ValueError, match="price_effective_window_overlaps"):
            await create_price_snapshot(
                db, **{**arguments, "price_per_unit": Decimal("0.000002")},
                effective_from=now + timedelta(hours=1), effective_to=None,
            )
    await engine.dispose()
