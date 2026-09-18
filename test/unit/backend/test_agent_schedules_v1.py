"""Durable schedules create ordinary Runs without forging user-authored input."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.deps import CurrentAccount
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint, AgentMember
from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, UserIdentity, UserSession
from app.models.runtime_v1 import DurableRunner, Message, MessageMention, Run, ToolInvocation
from app.models.schedule_v1 import AgentSchedule, AgentScheduleFire, RunnerScheduleSnapshot
from app.models.tenant_v1 import Tenant, TenantMembership
from app.services.schedule_service import (
    claim_dispatchable_fire,
    claim_due_schedule,
    cron_next,
    dispatch_fire_claim,
    initial_fire_at,
    materialize_schedule_claim,
    reconcile_one_fire,
)
import app.services.schedule_service as schedule_service
from app.api.schedules import update_schedule
from app.schemas.schedule_v1 import AgentSchedulePatch
from app.core.tool_registry.base import RuntimeContext
from app.core.tool_registry.builtin.schedule_task import ScheduleTaskTool
from app.runtime.action_dispatcher import ActionDispatcher
from app.runtime.protocol import ToolCallItem
from app.workers.runtime_worker import _scheduled_current_input, _scheduled_execution_rules
from app.services.schedule_definition_service import load_schedule_definition
from app.services.principal_ref_service import encode_account_ref


pytestmark = pytest.mark.unit


def test_cron_is_interpreted_in_the_declared_iana_timezone():
    winter = cron_next(
        "0 9 * * *", after_utc=datetime(2026, 1, 1),
        timezone_name="America/New_York",
    )
    summer = cron_next(
        "0 9 * * *", after_utc=datetime(2026, 7, 1),
        timezone_name="America/New_York",
    )
    shanghai = cron_next(
        "0 9 * * *", after_utc=datetime(2026, 1, 1),
        timezone_name="Asia/Shanghai",
    )
    assert winter.hour == 14
    assert summer.hour == 13
    assert shanghai.hour == 1


def test_one_time_schedule_requires_offset_and_future_time():
    now = datetime(2026, 8, 16, 10, 0, 0)
    value = initial_fire_at(
        schedule_type="once", timezone_name="Asia/Taipei", now=now,
        run_at=datetime(2026, 8, 16, 18, 30, tzinfo=timezone(timedelta(hours=8))),
    )
    assert value == datetime(2026, 8, 16, 10, 30)
    with pytest.raises(ValueError, match="future"):
        initial_fire_at(
            schedule_type="once", timezone_name="Asia/Taipei", now=now,
            run_at=datetime(2026, 8, 16, 9, 59, tzinfo=timezone.utc),
        )


def test_scheduled_execution_prompt_makes_the_current_instruction_authoritative():
    rules = _scheduled_execution_rules()
    current = _scheduled_current_input(
        "Reply with the current platform time",
        {
            "schedule_id": "schedule-1", "schedule_fire_id": "fire-1",
            "scheduled_for": "2026-08-16T11:00:00",
        },
    )
    assert "not a new user request to configure a schedule" in rules
    assert "Do not recreate" in rules
    assert "Active scheduled instruction" in current
    assert "Reply with the current platform time" in current
    assert '"schedule_fire_id": "fire-1"' in current


@pytest.mark.asyncio
async def test_dispatcher_rejects_scheduled_schedule_mutation_before_durable_wait():
    dispatcher = ActionDispatcher([{
        "name": "schedule_task",
        "input_schema": {
            "type": "object",
            "properties": {"operation": {"type": "string"}},
            "required": ["operation"],
        },
        "output_schema": {}, "side_effect": "non_idempotent",
        "confirmation_policy": "per_call", "execution_mode": "durable_async",
    }])
    result = (await dispatcher.dispatch_batch(
        [ToolCallItem(
            call_id="recursive-call", name="schedule_task",
            arguments={"operation": "create"}, index=0,
        )],
        capability_snapshot={"effective_tool_keys": ["schedule_task"]},
        runtime_context=RuntimeContext(
            tenant_id="tenant-1", session_id="session-1",
            runner_id="runner-1", user_id="user-1", agent_id="agent-1",
            authentication_source="schedule",
        ),
    ))[0]
    assert result.result.status == "failed"
    assert result.result.error.code == "schedule_recursive_mutation_denied"
    assert result.durable_wait is False


async def _seed(factory, *, next_fire_at: datetime) -> str:
    tenant_id, account_id, agent_id, version_id, schedule_id = (uuid7() for _ in range(5))
    now = utcnow()
    async with factory.begin() as db:
        db.add_all([
            Tenant(id=tenant_id, slug=f"tenant-{tenant_id}", name="Test", status="active"),
            UserIdentity(user_id="schedule-user", status="active"),
            PlatformAccount(
                id=account_id, tenant_id=tenant_id, user_id="schedule-user",
                display_name="Scheduler User", status="active", auth_revision=1,
            ),
            AgentBlueprint(
                id=agent_id, tenant_id=tenant_id, owner_account_id=account_id,
                visibility="private", status="published",
                draft_version_id=None, current_published_version_id=version_id,
                next_version_no=2,
            ),
        ])
        await db.flush()
        db.add_all([
            TenantMembership(
                id=uuid7(), tenant_id=tenant_id, account_id=account_id,
                role_key="member", status="active",
            ),
            AgentAssemblyVersion(
                id=version_id, agent_id=agent_id, version_no=1,
                publication_status="published", name="Reporter",
                description="Reports facts", instructions="Return a concise report",
                group_enabled=False, discovery_config={}, collaboration_config={},
                created_by_account_id=account_id, published_at=now,
                content_hash="a" * 64, immutable_at=now,
            ),
            AgentSchedule(
                id=schedule_id, tenant_id=tenant_id, account_id=account_id,
                user_id="schedule-user", legacy_agent_id=agent_id,
                legacy_name="Daily report",
                legacy_prompt="Summarize the latest evidence",
                schedule_type="interval", interval_seconds=3600,
                run_at=None, cron_expression=None, timezone="Asia/Taipei",
                next_fire_at=next_fire_at, end_at=None,
                legacy_context_mode="new_session",
                legacy_version_policy="latest_published",
                legacy_pinned_agent_version_id=None,
                overlap_policy="skip", misfire_policy="fire_once",
                misfire_grace_seconds=300, status="active",
                max_occurrences=None, occurrence_count=0, total_fire_count=0,
            ),
        ])
    return schedule_id


@pytest.mark.asyncio
async def test_schedule_api_updates_only_trigger_and_normalizes_offset_to_utc():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded_id = await _seed(factory, next_fire_at=utcnow() + timedelta(hours=1))
    async with factory() as db:
        seeded = await db.get(AgentSchedule, seeded_id)
        account = await db.get(PlatformAccount, seeded.account_id)
    current = CurrentAccount(account=account, roles=frozenset({"user"}))
    body = AgentSchedulePatch(
        schedule_type="once",
        run_at=datetime.now(timezone.utc) + timedelta(hours=2),
        cron_expression=None, interval_seconds=None, timezone="Asia/Taipei",
    )
    async with factory() as db:
        response = await update_schedule(
            schedule_id=seeded_id, body=body, if_match='W/"1"',
            db=db, current=current,
        )
        assert response.schedule_type == "once"
        assert response.next_fire_at is not None
        assert response.next_fire_at.tzinfo is None
        assert response.next_fire_local.endswith("+08:00")
    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_schedule_tool_creates_confirmed_owned_continuation_idempotently(
    monkeypatch,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(schedule_service, "AsyncSessionLocal", factory)
    seeded_id = await _seed(factory, next_fire_at=utcnow() + timedelta(hours=1))
    source_session_id = uuid7()
    async with factory.begin() as db:
        seeded = await db.get(AgentSchedule, seeded_id)
        recipient_account_id = uuid7()
        db.add(UserIdentity(user_id="recipient-user", status="active"))
        await db.flush()
        db.add(PlatformAccount(
            id=recipient_account_id, tenant_id=seeded.tenant_id,
            user_id="recipient-user", display_name="Report Recipient",
            status="active", auth_revision=1,
        ))
        await db.flush()
        db.add_all([
            TenantMembership(
                id=uuid7(), tenant_id=seeded.tenant_id,
                account_id=recipient_account_id, role_key="member", status="active",
            ),
            AgentMember(
                id=uuid7(), tenant_id=seeded.tenant_id,
                agent_id=seeded.legacy_agent_id,
                account_id=recipient_account_id,
                granted_by_account_id=seeded.account_id,
                granted_at=utcnow(), status="active",
            ),
        ])
        db.add(UserSession(
            id=source_session_id, tenant_id=seeded.tenant_id,
            account_id=seeded.account_id, user_id=seeded.user_id,
            entry_agent_id=seeded.legacy_agent_id, status="active", origin_type="web",
            permission_mode="direct", permission_revision=1,
        ))
        db.add(DurableRunner(
            id="source-runner", tenant_id=seeded.tenant_id,
            work_item_id="source-work", run_id="source-run",
            session_id=source_session_id, agent_id=seeded.legacy_agent_id,
            agent_version_id=(await db.get(
                AgentBlueprint, seeded.legacy_agent_id,
            )).current_published_version_id,
            execution_epoch=1, fencing_token=1, status="running",
            principal_snapshot={}, assembly_snapshot={}, prompt_manifest={},
            input_snapshot={},
        ))
        db.add(ToolInvocation(
            id="schedule-tool-invocation", tenant_id=seeded.tenant_id,
            account_id=seeded.account_id, runner_id="source-runner",
            work_item_id="source-work", run_id="source-run",
            session_id=source_session_id, platform_tool_id="schedule-tool",
            tool_version_id="schedule-tool-version",
            tool_version_content_hash="d" * 64, tool_key="schedule_task",
            execution_mode="inline", status="running", arguments={},
            arguments_hash="e" * 64, business_idempotency_key="schedule-create",
            principal_hash="f" * 64, authorization_snapshot={}, attempt_count=1,
            execution_epoch=1,
        ))
    async with factory() as db:
        seeded = await db.get(AgentSchedule, seeded_id)
        context = RuntimeContext(
            tenant_id=seeded.tenant_id, account_id=seeded.account_id,
            session_id=source_session_id, runner_id="source-runner",
            user_id=seeded.user_id, agent_id=seeded.legacy_agent_id,
            tool_invocation_id="schedule-tool-invocation",
            runtime_clock={"timezone": "Asia/Taipei"},
        )
    args = {
        "operation": "create", "name": "Two minute follow-up",
        "instruction": "Continue reviewing the evidence in this conversation",
        "context_mode": "continue_session",
        "trigger": {"type": "delay", "delay_seconds": 120},
        "principals": [{
            "principal_ref": encode_account_ref(
                tenant_id=context.tenant_id, agent_id=context.agent_id,
                account_id=recipient_account_id,
            ),
            "role": "recipient",
        }],
    }
    tool = ScheduleTaskTool()
    before = utcnow()
    async with factory.begin() as db:
        created = await tool.execute(args, context, db=db)
        replayed = await tool.execute(args, context, db=db)
    assert created.status == "succeeded"
    assert replayed.status == "succeeded"
    assert replayed.data["replayed"] is True
    assert created.data["schedule"]["id"] == replayed.data["schedule"]["id"]
    async with factory() as db:
        item = await db.get(AgentSchedule, created.data["schedule"]["id"])
        definition = await load_schedule_definition(db, item)
        assert definition.context_mode == "continue_session"
        assert definition.source_session_id == source_session_id
        assert item.definition_tool_invocation_id == "schedule-tool-invocation"
        assert item.legacy_prompt is None
        assert definition.principals[0].account_id == recipient_account_id
        assert definition.principals[0].role == "recipient"
        assert item.timezone == "Asia/Taipei"
        assert before + timedelta(seconds=119) <= item.next_fire_at
        assert item.next_fire_at <= before + timedelta(seconds=121)
    async with factory.begin() as db:
        item = await db.get(AgentSchedule, created.data["schedule"]["id"])
        item.next_fire_at = utcnow() - timedelta(seconds=1)
    schedule_claim = await claim_due_schedule("scheduler:principal-test")
    assert schedule_claim is not None
    assert await materialize_schedule_claim(schedule_claim) is True
    fire_claim = await claim_dispatchable_fire("scheduler:principal-test")
    assert fire_claim is not None
    assert await dispatch_fire_claim(fire_claim) is True
    async with factory() as db:
        fire = await db.get(AgentScheduleFire, fire_claim.fire_id)
        runner = await db.get(DurableRunner, fire.runner_id)
        mention = await db.scalar(select(MessageMention).where(
            MessageMention.message_id == fire.input_message_id,
        ))
        resolved = runner.input_snapshot["resolved_user_mentions"][0]
        assert mention.target_account_id == recipient_account_id
        assert resolved["account_id"] == recipient_account_id
        assert resolved["role"] == "recipient"
        assert resolved["principal_ref"].startswith("usr_")
    await engine.dispose()


@pytest.mark.asyncio
async def test_max_occurrences_completes_after_exactly_ten_materializations(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(schedule_service, "AsyncSessionLocal", factory)
    schedule_id = await _seed(factory, next_fire_at=utcnow() - timedelta(seconds=1))
    async with factory.begin() as db:
        schedule = await db.get(AgentSchedule, schedule_id)
        schedule.max_occurrences = 10
    for occurrence in range(1, 11):
        claim = await claim_due_schedule(f"scheduler:occurrence-{occurrence}")
        assert claim is not None
        assert await materialize_schedule_claim(claim) is True
        async with factory.begin() as db:
            schedule = await db.get(AgentSchedule, schedule_id)
            assert schedule.occurrence_count == occurrence
            if occurrence < 10:
                assert schedule.status == "active"
                schedule.next_fire_at = utcnow() - timedelta(seconds=1)
            else:
                assert schedule.status == "completed"
                assert schedule.next_fire_at is None
    assert await claim_due_schedule("scheduler:eleventh") is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_schedule_triggered_run_cannot_recursively_create_schedule():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded_id = await _seed(factory, next_fire_at=utcnow() + timedelta(hours=1))
    source_session_id = uuid7()
    async with factory.begin() as db:
        seeded = await db.get(AgentSchedule, seeded_id)
        db.add(UserSession(
            id=source_session_id, tenant_id=seeded.tenant_id,
            account_id=seeded.account_id, user_id=seeded.user_id,
            entry_agent_id=seeded.legacy_agent_id, status="active", origin_type="schedule",
            origin_schedule_id=seeded.id, permission_mode="direct",
            permission_revision=1,
        ))
    async with factory.begin() as db:
        seeded = await db.get(AgentSchedule, seeded_id)
        result = await ScheduleTaskTool().execute({
            "operation": "create", "instruction": "Create another task",
            "trigger": {"type": "delay", "delay_seconds": 120},
        }, RuntimeContext(
            tenant_id=seeded.tenant_id, account_id=seeded.account_id,
            session_id=source_session_id, runner_id="scheduled-runner",
            user_id=seeded.user_id, agent_id=seeded.legacy_agent_id,
            tool_invocation_id="recursive-invocation",
            authentication_source="schedule",
        ), db=db)
        assert result.status == "failed"
        assert result.error.code == "schedule_recursive_mutation_denied"
    await engine.dispose()


@pytest.mark.asyncio
async def test_fire_is_idempotent_and_dispatches_platform_authored_normal_run(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(schedule_service, "AsyncSessionLocal", factory)
    schedule_id = await _seed(factory, next_fire_at=utcnow() - timedelta(seconds=1))

    claim = await claim_due_schedule("scheduler:test")
    assert claim is not None
    assert await materialize_schedule_claim(claim) is True
    assert await materialize_schedule_claim(claim) is False
    async with factory() as db:
        fires = list(await db.scalars(select(AgentScheduleFire).where(
            AgentScheduleFire.schedule_id == schedule_id,
        )))
        assert len(fires) == 1
        assert fires[0].status == "queued"

    fire_claim = await claim_dispatchable_fire("scheduler:test")
    assert fire_claim is not None
    assert await dispatch_fire_claim(fire_claim) is True
    async with factory() as db:
        fire = await db.get(AgentScheduleFire, fire_claim.fire_id)
        session = await db.get(UserSession, fire.session_id)
        directive = await db.get(Message, fire.input_message_id)
        snapshot = await db.get(RunnerScheduleSnapshot, fire.runner_id)
        run = await db.get(Run, fire.run_id)
        assert fire.status == "running"
        assert session.origin_type == "schedule"
        assert session.origin_schedule_id == schedule_id
        assert directive.sender_type == "system"
        assert directive.sender_user_id is None
        assert directive.role == "user"
        assert run.mode == "chat"
        assert snapshot.definition_hash == fire.definition_hash

    async with factory.begin() as db:
        fire = await db.get(AgentScheduleFire, fire_claim.fire_id)
        run = await db.get(Run, fire.run_id)
        run.status = "completed"
        run.completed_at = utcnow()
        run.version += 1
    assert await reconcile_one_fire() is True
    async with factory() as db:
        fire = await db.get(AgentScheduleFire, fire_claim.fire_id)
        assert fire.status == "succeeded"
        assert fire.notification_read_at is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_fire_can_continue_the_original_conversation(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(schedule_service, "AsyncSessionLocal", factory)
    schedule_id = await _seed(factory, next_fire_at=utcnow() - timedelta(seconds=1))
    source_session_id = uuid7()
    async with factory.begin() as db:
        schedule = await db.get(AgentSchedule, schedule_id)
        schedule.legacy_context_mode = "continue_session"
        schedule.legacy_source_session_id = source_session_id
        db.add(UserSession(
            id=source_session_id, tenant_id=schedule.tenant_id,
            account_id=schedule.account_id, user_id=schedule.user_id,
            entry_agent_id=schedule.legacy_agent_id, status="active", origin_type="web",
            permission_mode="direct", permission_revision=1,
        ))

    schedule_claim = await claim_due_schedule("scheduler:test")
    assert schedule_claim is not None
    assert await materialize_schedule_claim(schedule_claim) is True
    fire_claim = await claim_dispatchable_fire("scheduler:test")
    assert fire_claim is not None
    assert await dispatch_fire_claim(fire_claim) is True
    async with factory() as db:
        fire = await db.get(AgentScheduleFire, fire_claim.fire_id)
        directive = await db.get(Message, fire.input_message_id)
        assert fire.definition_hash is not None
        assert fire.session_id == source_session_id
        assert directive.session_id == source_session_id
        assert directive.sender_type == "system"
    await engine.dispose()


@pytest.mark.asyncio
async def test_overlapping_skip_is_ledgered_without_second_run(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(schedule_service, "AsyncSessionLocal", factory)
    schedule_id = await _seed(factory, next_fire_at=utcnow() - timedelta(seconds=1))
    async with factory.begin() as db:
        schedule = await db.get(AgentSchedule, schedule_id)
        db.add(AgentScheduleFire(
            id=uuid7(), schedule_id=schedule.id, tenant_id=schedule.tenant_id,
            account_id=schedule.account_id, trigger_kind="manual",
            operation_key="manual:active", scheduled_for=utcnow(), status="running",
            schedule_version=1, legacy_prompt_snapshot=schedule.legacy_prompt,
            legacy_prompt_hash="b" * 64, timezone_snapshot=schedule.timezone,
            legacy_version_policy_snapshot=schedule.legacy_version_policy,
            session_id=None, run_id="already-running", runner_id="active-runner",
            fired_at=utcnow(),
        ))

    claim = await claim_due_schedule("scheduler:test")
    assert claim is not None
    assert await materialize_schedule_claim(claim) is True
    async with factory() as db:
        skipped = await db.scalar(select(AgentScheduleFire).where(
            AgentScheduleFire.schedule_id == schedule_id,
            AgentScheduleFire.trigger_kind == "scheduled",
        ))
        assert skipped.status == "skipped"
        assert skipped.reason_code == "overlap_skipped"
        assert skipped.completed_at is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_dispatch_lease_is_recovered_without_bulk_update(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(schedule_service, "AsyncSessionLocal", factory)
    schedule_id = await _seed(factory, next_fire_at=utcnow() + timedelta(hours=1))
    expired_id, active_id = uuid7(), uuid7()
    async with factory.begin() as db:
        schedule = await db.get(AgentSchedule, schedule_id)
        common = {
            "schedule_id": schedule.id,
            "tenant_id": schedule.tenant_id,
            "account_id": schedule.account_id,
            "trigger_kind": "manual",
            "scheduled_for": utcnow(),
            "status": "dispatching",
            "schedule_version": 1,
            "legacy_prompt_snapshot": schedule.legacy_prompt,
            "legacy_prompt_hash": "c" * 64,
            "timezone_snapshot": schedule.timezone,
            "legacy_version_policy_snapshot": schedule.legacy_version_policy,
            "run_id": None,
        }
        db.add_all([
            AgentScheduleFire(
                id=expired_id, operation_key="manual:expired",
                lease_owner="dead-worker", lease_token=uuid7(),
                lease_expires_at=utcnow() - timedelta(seconds=1), **common,
            ),
            AgentScheduleFire(
                id=active_id, operation_key="manual:active-lease",
                lease_owner="live-worker", lease_token=uuid7(),
                lease_expires_at=utcnow() + timedelta(minutes=1), **common,
            ),
        ])

    claim = await claim_dispatchable_fire("scheduler:replacement")
    assert claim is not None
    assert claim.fire_id == expired_id
    async with factory() as db:
        expired = await db.get(AgentScheduleFire, expired_id)
        active = await db.get(AgentScheduleFire, active_id)
        assert expired.status == "dispatching"
        assert expired.reason_code == "dispatch_lease_recovered"
        assert expired.lease_owner == "scheduler:replacement"
        assert active.status == "dispatching"
        assert active.lease_owner == "live-worker"
    await engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_deadlock_is_requeued_instead_of_failed(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(schedule_service, "AsyncSessionLocal", factory)
    schedule_id = await _seed(factory, next_fire_at=utcnow() - timedelta(seconds=1))
    schedule_claim = await claim_due_schedule("scheduler:test")
    assert schedule_claim is not None
    assert await materialize_schedule_claim(schedule_claim) is True
    fire_claim = await claim_dispatchable_fire("scheduler:test")
    assert fire_claim is not None

    async def deadlocked_create(*args, **kwargs):
        raise OperationalError(
            "INSERT INTO user_session", {}, Exception(1213, "deadlock")
        )

    monkeypatch.setattr(schedule_service, "create_user_session", deadlocked_create)
    assert await dispatch_fire_claim(fire_claim) is False
    async with factory() as db:
        fire = await db.get(AgentScheduleFire, fire_claim.fire_id)
        schedule = await db.get(AgentSchedule, schedule_id)
        assert fire.status == "queued"
        assert fire.reason_code == "dispatch_database_retry"
        assert fire.completed_at is None
        assert fire.lease_token is None
        assert schedule.last_error_code is None
    await engine.dispose()
