"""Multi-instance-safe schedule materialization, dispatch, and reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, PlatformAccountRole, UserSession
from app.models.runtime_v1 import Message, Run
from app.models.schedule_v1 import AgentSchedule, AgentScheduleFire, RunnerScheduleSnapshot
from app.models.tenant_v1 import Tenant, TenantMembership
from app.services.chat_service import AgentUnavailable, create_user_session, enqueue_chat_message
from app.services.agent_access_service import has_agent_use_access
from app.services.agent_people_service import resolve_agent_person
from app.services.principal_ref_service import encode_account_ref
from app.services.schedule_definition_service import (
    ScheduleDefinition,
    load_schedule_definition,
)


TERMINAL_FIRE_STATUSES = frozenset({"succeeded", "failed", "skipped", "cancelled"})
ACTIVE_FIRE_STATUSES = frozenset({"dispatching", "running"})


@dataclass(frozen=True)
class ScheduleClaim:
    schedule_id: str
    lease_token: str


@dataclass(frozen=True)
class FireClaim:
    fire_id: str
    lease_token: str


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown IANA timezone: {name}") from error


def utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def local_iso(value: datetime, timezone_name: str) -> str:
    return value.replace(tzinfo=timezone.utc).astimezone(zone(timezone_name)).isoformat()


def validate_cron_expression(expression: str) -> str:
    value = " ".join(expression.strip().split())
    if len(value.split()) != 5 or not croniter.is_valid(value):
        raise ValueError("cron expression must contain five valid fields")
    return value


def cron_next(expression: str, *, after_utc: datetime, timezone_name: str) -> datetime:
    local_base = after_utc.replace(tzinfo=timezone.utc).astimezone(zone(timezone_name))
    next_local = croniter(validate_cron_expression(expression), local_base).get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=zone(timezone_name))
    return next_local.astimezone(timezone.utc).replace(tzinfo=None)


def initial_fire_at(
    *, schedule_type: str, timezone_name: str, now: datetime,
    run_at: datetime | None = None, cron_expression: str | None = None,
    interval_seconds: int | None = None,
) -> datetime:
    zone(timezone_name)
    if schedule_type == "once":
        if run_at is None:
            raise ValueError("run_at is required for a one-time schedule")
        value = utc_naive(run_at)
        if value <= now:
            raise ValueError("run_at must be in the future")
        return value
    if schedule_type == "cron":
        if not cron_expression:
            raise ValueError("cron_expression is required for a cron schedule")
        return cron_next(cron_expression, after_utc=now, timezone_name=timezone_name)
    if schedule_type == "interval":
        if interval_seconds is None or interval_seconds < 60:
            raise ValueError("interval_seconds must be at least 60")
        return now + timedelta(seconds=interval_seconds)
    raise ValueError("schedule_type must be once, cron, or interval")


def next_fire_after(schedule: AgentSchedule, scheduled_for: datetime) -> datetime | None:
    if schedule.schedule_type == "once":
        return None
    if schedule.schedule_type == "cron":
        return cron_next(
            schedule.cron_expression or "", after_utc=scheduled_for,
            timezone_name=schedule.timezone,
        )
    if schedule.schedule_type == "interval" and schedule.interval_seconds:
        return scheduled_for + timedelta(seconds=schedule.interval_seconds)
    raise ValueError("invalid persisted schedule shape")


def advance_schedule(
    schedule: AgentSchedule, *, scheduled_for: datetime, now: datetime,
    coalesce_missed: bool,
) -> None:
    exhausted = (
        schedule.max_occurrences is not None
        and schedule.occurrence_count >= schedule.max_occurrences
    )
    next_at = None if exhausted else next_fire_after(schedule, scheduled_for)
    if coalesce_missed and next_at is not None:
        if schedule.schedule_type == "cron":
            next_at = cron_next(
                schedule.cron_expression or "", after_utc=now,
                timezone_name=schedule.timezone,
            )
        elif schedule.schedule_type == "interval" and schedule.interval_seconds:
            elapsed = max(0.0, (now - scheduled_for).total_seconds())
            steps = int(elapsed // schedule.interval_seconds) + 1
            next_at = scheduled_for + timedelta(
                seconds=steps * schedule.interval_seconds
            )
    if next_at is not None and schedule.end_at is not None and next_at > schedule.end_at:
        next_at = None
    schedule.next_fire_at = next_at
    schedule.last_scheduled_for = scheduled_for
    schedule.status = "completed" if next_at is None else schedule.status
    schedule.lease_owner = None
    schedule.lease_token = None
    schedule.lease_expires_at = None
    schedule.version += 1


async def claim_due_schedule(worker_id: str) -> ScheduleClaim | None:
    now = utcnow()
    async with AsyncSessionLocal.begin() as db:
        schedule = await db.scalar(select(AgentSchedule).where(
            AgentSchedule.status == "active",
            AgentSchedule.next_fire_at.is_not(None),
            AgentSchedule.next_fire_at <= now,
            or_(
                AgentSchedule.lease_expires_at.is_(None),
                AgentSchedule.lease_expires_at <= now,
            ),
        ).order_by(AgentSchedule.next_fire_at, AgentSchedule.id).with_for_update(
            skip_locked=True
        ).limit(1))
        if schedule is None:
            return None
        token = uuid7()
        schedule.lease_owner = worker_id
        schedule.lease_token = token
        schedule.lease_expires_at = now + timedelta(
            seconds=settings.scheduler_claim_lease_seconds
        )
        return ScheduleClaim(schedule.id, token)


async def materialize_schedule_claim(claim: ScheduleClaim) -> bool:
    """Create exactly one ledger row and advance the definition atomically."""

    now = utcnow()
    async with AsyncSessionLocal.begin() as db:
        schedule = await db.scalar(select(AgentSchedule).where(
            AgentSchedule.id == claim.schedule_id,
            AgentSchedule.lease_token == claim.lease_token,
        ).with_for_update())
        if schedule is None:
            return False
        if (
            schedule.status != "active"
            or schedule.next_fire_at is None or schedule.next_fire_at > now
        ):
            schedule.lease_owner = None
            schedule.lease_token = None
            schedule.lease_expires_at = None
            return False
        scheduled_for = schedule.next_fire_at
        operation_key = f"scheduled:{scheduled_for.isoformat(timespec='microseconds')}"
        existing = await db.scalar(select(AgentScheduleFire).where(
            AgentScheduleFire.schedule_id == schedule.id,
            AgentScheduleFire.operation_key == operation_key,
        ))
        late_seconds = max(0.0, (now - scheduled_for).total_seconds())
        is_misfire = late_seconds > schedule.misfire_grace_seconds
        active_exists = bool(await db.scalar(select(exists().where(
            AgentScheduleFire.schedule_id == schedule.id,
            AgentScheduleFire.status.in_(ACTIVE_FIRE_STATUSES),
        ))))
        deferred_exists = bool(await db.scalar(select(exists().where(
            AgentScheduleFire.schedule_id == schedule.id,
            AgentScheduleFire.status == "deferred",
        ))))

        if existing is None:
            status = "queued"
            reason = None
            if is_misfire and schedule.misfire_policy == "skip":
                status, reason = "skipped", "misfire_grace_exceeded"
            elif active_exists and schedule.overlap_policy == "skip":
                status, reason = "skipped", "overlap_skipped"
            elif active_exists and schedule.overlap_policy == "queue_one":
                if deferred_exists:
                    status, reason = "skipped", "overlap_coalesced"
                else:
                    status, reason = "deferred", "overlap_deferred"
            fire = AgentScheduleFire(
                id=uuid7(), schedule_id=schedule.id, tenant_id=schedule.tenant_id,
                account_id=schedule.account_id, trigger_kind="scheduled",
                operation_key=operation_key, scheduled_for=scheduled_for,
                status=status, reason_code=reason,
                schedule_version=schedule.version,
                definition_tool_invocation_id=schedule.definition_tool_invocation_id,
                timezone_snapshot=schedule.timezone,
                occurrence_no=schedule.occurrence_count + 1,
                completed_at=now if status == "skipped" else None,
            )
            definition = await load_schedule_definition(db, schedule)
            fire.definition_hash = definition.content_hash
            db.add(fire)
            schedule.occurrence_count += 1
        advance_schedule(
            schedule, scheduled_for=scheduled_for, now=now,
            # Never materialize an unbounded backlog after downtime or while a
            # prior Run overlaps. One ledger row explains the coalesced gap.
            coalesce_missed=is_misfire or active_exists,
        )
        return existing is None


async def claim_dispatchable_fire(worker_id: str) -> FireClaim | None:
    now = utcnow()
    async with AsyncSessionLocal.begin() as db:
        fire = await db.scalar(select(AgentScheduleFire).where(
            AgentScheduleFire.status == "dispatching",
            AgentScheduleFire.lease_expires_at <= now,
            AgentScheduleFire.run_id.is_(None),
        ).order_by(
            AgentScheduleFire.lease_expires_at, AgentScheduleFire.id,
        ).with_for_update(skip_locked=True).limit(1))
        if fire is not None:
            # Recover only a row whose primary record is not owned by a live
            # dispatcher.  A bulk UPDATE can lock the nullable unique run_id
            # index before the primary row while dispatch settlement takes the
            # reverse order, which produces an avoidable MySQL 1213 deadlock.
            fire.status = "queued"
            fire.reason_code = "dispatch_lease_recovered"
            fire.lease_owner = None
            fire.lease_token = None
            fire.lease_expires_at = None

        other = AgentScheduleFire.__table__.alias("active_schedule_fire")
        no_active_for_deferred = ~exists(select(1).select_from(other).where(and_(
            other.c.schedule_id == AgentScheduleFire.schedule_id,
            other.c.id != AgentScheduleFire.id,
            other.c.status.in_(ACTIVE_FIRE_STATUSES),
        )))
        fire = await db.scalar(select(AgentScheduleFire).where(
            AgentScheduleFire.run_id.is_(None),
            or_(
                AgentScheduleFire.status == "queued",
                and_(AgentScheduleFire.status == "deferred", no_active_for_deferred),
            ),
        ).order_by(AgentScheduleFire.scheduled_for, AgentScheduleFire.id).with_for_update(
            skip_locked=True
        ).limit(1))
        if fire is None:
            return None
        token = uuid7()
        fire.status = "dispatching"
        fire.lease_owner = worker_id
        fire.lease_token = token
        fire.lease_expires_at = now + timedelta(
            seconds=settings.scheduler_claim_lease_seconds
        )
        return FireClaim(fire.id, token)


async def _current_authority(db, schedule: AgentSchedule):
    account = await db.get(PlatformAccount, schedule.account_id)
    tenant = await db.get(Tenant, schedule.tenant_id)
    membership = await db.scalar(select(TenantMembership).where(
        TenantMembership.tenant_id == schedule.tenant_id,
        TenantMembership.account_id == schedule.account_id,
        TenantMembership.status == "active",
    ))
    if (
        account is None or account.status != "active"
        or account.user_id != schedule.user_id
        or tenant is None or tenant.status != "active"
        or membership is None
    ):
        raise AgentUnavailable("Schedule owner authority is unavailable")
    return account


async def _resolved_agent_version(
    db, schedule: AgentSchedule, definition: ScheduleDefinition,
) -> str:
    agent = await db.scalar(select(AgentBlueprint).where(
        AgentBlueprint.id == definition.agent_id,
        AgentBlueprint.tenant_id == schedule.tenant_id,
    ))
    if agent is None or agent.status != "published" or not agent.current_published_version_id:
        raise AgentUnavailable("Scheduled Agent is unavailable")
    roles = set(await db.scalars(select(PlatformAccountRole.role_key).where(
        PlatformAccountRole.account_id == schedule.account_id,
    )))
    if not await has_agent_use_access(
        db, agent=agent, account_id=schedule.account_id, roles=roles,
    ):
        raise AgentUnavailable("Scheduled Agent is no longer visible")
    version_id = (
        definition.pinned_agent_version_id
        if definition.version_policy == "pinned"
        else agent.current_published_version_id
    )
    version = await db.get(AgentAssemblyVersion, version_id)
    if (
        version is None or version.agent_id != agent.id
        or version.publication_status != "published"
    ):
        raise AgentUnavailable("Scheduled Agent version is unavailable")
    return version.id


async def _resolved_principals(
    db, schedule: AgentSchedule, definition: ScheduleDefinition,
) -> list[dict]:
    resolved = []
    for principal in definition.principals:
        person = await resolve_agent_person(
            db, tenant_id=schedule.tenant_id, agent_id=definition.agent_id,
            requester_account_id=schedule.account_id,
            target_account_id=principal.account_id,
        )
        if person is None or person.user_id != principal.user_id:
            raise AgentUnavailable(
                f"Scheduled principal is no longer contactable: {principal.display_name}"
            )
        resolved.append({
            "account_id": person.account_id,
            "display_name": person.display_name,
            "handle": person.handle,
            "role": principal.role,
            "principal_ref": encode_account_ref(
                tenant_id=schedule.tenant_id, agent_id=definition.agent_id,
                account_id=person.account_id,
            ),
        })
    return resolved


async def dispatch_fire_claim(claim: FireClaim) -> bool:
    """Turn one fire into a normal Chat Run in a single DB transaction."""

    try:
        async with AsyncSessionLocal.begin() as db:
            schedule_id = await db.scalar(select(AgentScheduleFire.schedule_id).where(
                AgentScheduleFire.id == claim.fire_id,
                AgentScheduleFire.lease_token == claim.lease_token,
                AgentScheduleFire.status == "dispatching",
            ))
            if schedule_id is None:
                return False
            # Every transaction that needs both records takes the definition
            # before its fire.  Pause/disable already use this order, so a
            # concurrent dispatch cannot form schedule <-> fire deadlocks.
            schedule = await db.scalar(select(AgentSchedule).where(
                AgentSchedule.id == schedule_id,
            ).with_for_update())
            fire = await db.scalar(select(AgentScheduleFire).where(
                AgentScheduleFire.id == claim.fire_id,
                AgentScheduleFire.lease_token == claim.lease_token,
                AgentScheduleFire.status == "dispatching",
                AgentScheduleFire.schedule_id == schedule_id,
            ).with_for_update())
            if fire is None:
                return False
            if schedule is None or schedule.status == "disabled":
                raise AgentUnavailable("Schedule is disabled")
            account = await _current_authority(db, schedule)
            definition = await load_schedule_definition(db, schedule, fire=fire)
            if definition.agent_id != (schedule.legacy_agent_id or definition.agent_id):
                raise AgentUnavailable("Schedule definition Agent does not match its legacy boundary")
            version_id = await _resolved_agent_version(db, schedule, definition)
            resolved_principals = await _resolved_principals(db, schedule, definition)
            if fire.definition_hash and fire.definition_hash != definition.content_hash:
                raise AgentUnavailable("Schedule definition integrity check failed")
            if definition.context_mode == "continue_session":
                session = await db.scalar(select(UserSession).where(
                    UserSession.id == definition.source_session_id,
                    UserSession.tenant_id == schedule.tenant_id,
                    UserSession.account_id == schedule.account_id,
                    UserSession.user_id == schedule.user_id,
                    UserSession.entry_agent_id == definition.agent_id,
                    UserSession.status == "active",
                ))
                if session is None:
                    raise AgentUnavailable(
                        "The source Session for scheduled continuation is unavailable"
                    )
            else:
                title_time = local_iso(fire.scheduled_for, fire.timezone_snapshot)[:16]
                session = await create_user_session(
                    db, tenant_id=schedule.tenant_id, account_id=schedule.account_id,
                    user_id=schedule.user_id, entry_agent_id=definition.agent_id,
                    origin_type="schedule", origin_schedule_id=schedule.id,
                    title=f"定时任务 · {definition.name} · {title_time}",
                    agent_version_id=version_id,
                    actor_type="service", actor_id="scheduler",
                )
            message, run, _, runner = await enqueue_chat_message(
                db, session_id=session.id, user_id=schedule.user_id,
                content=definition.instruction, idempotency_record_id=fire.id,
                agent_version_id=version_id, execution_kind="scheduled",
                sender_type="system", actor_type="service", actor_id="scheduler",
                authentication_source="schedule",
                input_metadata={
                    "schedule_id": schedule.id, "schedule_fire_id": fire.id,
                    "scheduled_for": fire.scheduled_for.isoformat(),
                    "schedule_version": fire.schedule_version,
                    "schedule_context_mode": definition.context_mode,
                    "schedule_definition_tool_invocation_id": definition.tool_invocation_id,
                    "schedule_definition_hash": definition.content_hash,
                },
                resolved_mentions=resolved_principals,
            )
            runner.principal_snapshot = {
                **dict(runner.principal_snapshot or {}),
                "schedule_id": schedule.id,
                "schedule_fire_id": fire.id,
                "account_auth_revision": account.auth_revision,
            }
            db.add(RunnerScheduleSnapshot(
                runner_id=runner.id, fire_id=fire.id, schedule_id=schedule.id,
                schedule_version=fire.schedule_version,
                scheduled_for=fire.scheduled_for, timezone=fire.timezone_snapshot,
                definition_tool_invocation_id=definition.tool_invocation_id,
                definition_hash=definition.content_hash,
                agent_version_id=version_id,
                account_auth_revision=account.auth_revision, created_at=utcnow(),
            ))
            now = utcnow()
            fire.agent_version_id = version_id
            fire.session_id = session.id
            fire.input_message_id = message.id
            fire.run_id = run.id
            fire.runner_id = runner.id
            fire.status = "running"
            fire.reason_code = None
            fire.fired_at = now
            fire.lease_owner = None
            fire.lease_token = None
            fire.lease_expires_at = None
            schedule.total_fire_count += 1
            schedule.last_fired_at = now
            schedule.last_error_code = None
            schedule.last_error_detail = None
            schedule.version += 1
        return True
    except Exception as error:
        async with AsyncSessionLocal.begin() as db:
            schedule_id = await db.scalar(select(AgentScheduleFire.schedule_id).where(
                AgentScheduleFire.id == claim.fire_id,
                AgentScheduleFire.lease_token == claim.lease_token,
            ))
            if schedule_id is None:
                return False
            schedule = await db.scalar(select(AgentSchedule).where(
                AgentSchedule.id == schedule_id,
            ).with_for_update())
            fire = await db.scalar(select(AgentScheduleFire).where(
                AgentScheduleFire.id == claim.fire_id,
                AgentScheduleFire.lease_token == claim.lease_token,
                AgentScheduleFire.schedule_id == schedule_id,
            ).with_for_update())
            if fire is not None and fire.run_id is None:
                now = utcnow()
                mysql_code = (
                    error.orig.args[0]
                    if isinstance(error, OperationalError)
                    and getattr(error, "orig", None)
                    and getattr(error.orig, "args", None)
                    else None
                )
                transient_database_error = mysql_code in {1205, 1213}
                fire.status = "queued" if transient_database_error else "failed"
                fire.reason_code = (
                    "dispatch_database_retry"
                    if transient_database_error else "dispatch_failed"
                )
                fire.error_code = (
                    None if transient_database_error else type(error).__name__[:100]
                )
                fire.error_detail = (
                    None if transient_database_error else str(error)[:4000]
                )
                fire.completed_at = None if transient_database_error else now
                fire.lease_owner = None
                fire.lease_token = None
                fire.lease_expires_at = None
                if schedule is not None and not transient_database_error:
                    schedule.last_error_code = fire.error_code
                    schedule.last_error_detail = fire.error_detail
                    if (
                        isinstance(error, AgentUnavailable)
                        and schedule.schedule_type != "once"
                    ):
                        schedule.status = "paused"
                    schedule.version += 1
        return False


async def reconcile_one_fire() -> bool:
    async with AsyncSessionLocal.begin() as db:
        candidate = (await db.execute(select(
            AgentScheduleFire.id,
            AgentScheduleFire.schedule_id,
        ).join(
            Run, Run.id == AgentScheduleFire.run_id,
        ).where(
            AgentScheduleFire.status == "running",
            Run.status.in_(("completed", "failed", "cancelled")),
        ).order_by(Run.completed_at, AgentScheduleFire.id).limit(1))).first()
        if candidate is None:
            return False
        schedule = await db.scalar(select(AgentSchedule).where(
            AgentSchedule.id == candidate.schedule_id,
        ).with_for_update())
        fire = await db.scalar(select(AgentScheduleFire).join(
            Run, Run.id == AgentScheduleFire.run_id,
        ).where(
            AgentScheduleFire.id == candidate.id,
            AgentScheduleFire.status == "running",
            Run.status.in_(("completed", "failed", "cancelled")),
        ).with_for_update())
        if fire is None:
            return False
        run = await db.get(Run, fire.run_id)
        if run is None:
            return False
        fire.status = {
            "completed": "succeeded", "failed": "failed", "cancelled": "cancelled",
        }[run.status]
        fire.reason_code = run.terminal_reason
        fire.error_code = run.terminal_reason if run.status == "failed" else None
        fire.completed_at = run.completed_at or utcnow()
        if schedule is not None and run.status == "failed":
            schedule.last_error_code = fire.error_code or "run_failed"
            schedule.last_error_detail = "Scheduled Agent Run failed"
            schedule.version += 1
        return True


async def cancel_pending_fires(db, schedule_id: str, *, reason: str) -> int:
    now = utcnow()
    result = await db.execute(update(AgentScheduleFire).where(
        AgentScheduleFire.schedule_id == schedule_id,
        AgentScheduleFire.status.in_(("queued", "deferred", "dispatching")),
        AgentScheduleFire.run_id.is_(None),
    ).values(
        status="cancelled", reason_code=reason, completed_at=now,
        lease_owner=None, lease_token=None, lease_expires_at=None,
    ))
    return int(result.rowcount or 0)


async def create_manual_fire(
    db, *, schedule: AgentSchedule, operation_key: str,
) -> AgentScheduleFire:
    existing = await db.scalar(select(AgentScheduleFire).where(
        AgentScheduleFire.schedule_id == schedule.id,
        AgentScheduleFire.operation_key == operation_key,
    ))
    if existing is not None:
        return existing
    fire = AgentScheduleFire(
        id=uuid7(), schedule_id=schedule.id, tenant_id=schedule.tenant_id,
        account_id=schedule.account_id, trigger_kind="manual",
        operation_key=operation_key, scheduled_for=utcnow(), status="queued",
        schedule_version=schedule.version,
        definition_tool_invocation_id=schedule.definition_tool_invocation_id,
        timezone_snapshot=schedule.timezone,
    )
    definition = await load_schedule_definition(db, schedule)
    fire.definition_hash = definition.content_hash
    db.add(fire)
    await db.flush()
    return fire
