from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import CurrentAccount, get_current_account
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord
from app.models.runtime_v1 import DurableRunner, Message
from app.models.schedule_v1 import AgentSchedule, AgentScheduleFire
from app.persistence.reliable_writes import PlatformAuditWriter
from app.schemas.schedule_v1 import (
    AgentScheduleCreate,
    AgentScheduleFireResponse,
    AgentSchedulePatch,
    AgentScheduleResponse,
)
from app.services.schedule_service import (
    cancel_pending_fires,
    create_manual_fire,
    initial_fire_at,
    local_iso,
    utc_naive,
    validate_cron_expression,
    zone,
)
from app.services.agent_access_service import has_agent_use_access


router = APIRouter(prefix="/schedules", tags=["agent-schedules"])


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _if_match(value: str | None) -> int:
    if value is None:
        raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    match = re.search(r"(\d+)", value)
    if match is None:
        raise HTTPException(status_code=400, detail={"code": "invalid_if_match"})
    return int(match.group(1))


async def _idempotency(
    db: AsyncSession, *, current: CurrentAccount, operation: str,
    key: str, request: dict,
) -> tuple[IdempotencyRecord, bool]:
    digest = _hash(request)
    record = await db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.principal_type == "account",
        IdempotencyRecord.principal_id == current.account.id,
        IdempotencyRecord.operation == operation,
        IdempotencyRecord.idempotency_key == key,
    ))
    if record is not None:
        if record.request_hash != digest:
            raise HTTPException(status_code=409, detail={"code": "idempotency_mismatch"})
        return record, True
    record = IdempotencyRecord(
        id=uuid7(), principal_type="account", principal_id=current.account.id,
        operation=operation, idempotency_key=key, request_hash=digest,
        expires_at=utcnow() + timedelta(days=7),
    )
    db.add(record)
    await db.flush()
    return record, False


async def _owned_schedule(
    db: AsyncSession, schedule_id: str, current: CurrentAccount, *, lock: bool = False,
) -> AgentSchedule:
    statement = select(AgentSchedule).where(
        AgentSchedule.id == schedule_id,
        AgentSchedule.tenant_id == current.tenant_id,
        AgentSchedule.account_id == current.account.id,
    )
    if lock:
        statement = statement.with_for_update()
    schedule = await db.scalar(statement)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule


async def _visible_agent(
    db: AsyncSession, agent_id: str, current: CurrentAccount,
) -> AgentBlueprint:
    agent = await db.scalar(select(AgentBlueprint).where(
        AgentBlueprint.id == agent_id,
        AgentBlueprint.tenant_id == current.tenant_id,
    ))
    if (
        agent is None
        or not await has_agent_use_access(
            db, agent=agent, account_id=current.account.id, roles=current.roles,
        )
        or agent.status != "published"
        or not agent.current_published_version_id
    ):
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


async def _agent_name(db: AsyncSession, agent: AgentBlueprint) -> str:
    version = await db.get(
        AgentAssemblyVersion,
        agent.current_published_version_id or agent.draft_version_id,
    )
    return version.name if version else agent.id


async def _schedule_view(db: AsyncSession, item: AgentSchedule) -> AgentScheduleResponse:
    agent = await db.get(AgentBlueprint, item.agent_id)
    name = await _agent_name(db, agent) if agent else item.agent_id
    return AgentScheduleResponse(
        id=item.id, agent_id=item.agent_id, agent_name=name,
        name=item.name, prompt=item.prompt, context_mode=item.context_mode,
        source_session_id=item.source_session_id,
        schedule_type=item.schedule_type,
        cron_expression=item.cron_expression, interval_seconds=item.interval_seconds,
        run_at=item.run_at, timezone=item.timezone, next_fire_at=item.next_fire_at,
        next_fire_local=(
            local_iso(item.next_fire_at, item.timezone) if item.next_fire_at else None
        ),
        end_at=item.end_at, version_policy=item.version_policy,
        pinned_agent_version_id=item.pinned_agent_version_id,
        overlap_policy=item.overlap_policy, misfire_policy=item.misfire_policy,
        misfire_grace_seconds=item.misfire_grace_seconds, status=item.status,
        total_fire_count=item.total_fire_count,
        last_scheduled_for=item.last_scheduled_for, last_fired_at=item.last_fired_at,
        last_error_code=item.last_error_code,
        last_error_detail=item.last_error_detail, version=item.version,
        created_at=item.created_at, updated_at=item.updated_at,
    )


async def _fire_view(db: AsyncSession, fire: AgentScheduleFire) -> AgentScheduleFireResponse:
    schedule = await db.get(AgentSchedule, fire.schedule_id)
    agent = await db.get(AgentBlueprint, schedule.agent_id) if schedule else None
    agent_name = await _agent_name(db, agent) if agent else "未知智能体"
    result_preview = None
    if fire.runner_id:
        runner = await db.get(DurableRunner, fire.runner_id)
        result = await db.get(Message, runner.result_message_id) if runner and runner.result_message_id else None
        result_preview = result.content[:500] if result else None
    return AgentScheduleFireResponse(
        id=fire.id, schedule_id=fire.schedule_id,
        schedule_name=schedule.name if schedule else "已删除任务",
        agent_id=schedule.agent_id if schedule else "", agent_name=agent_name,
        trigger_kind=fire.trigger_kind, context_mode=fire.context_mode_snapshot,
        scheduled_for=fire.scheduled_for,
        scheduled_for_local=local_iso(fire.scheduled_for, fire.timezone_snapshot),
        status=fire.status, reason_code=fire.reason_code,
        agent_version_id=fire.agent_version_id, session_id=fire.session_id,
        run_id=fire.run_id, runner_id=fire.runner_id,
        result_preview=result_preview, fired_at=fire.fired_at,
        completed_at=fire.completed_at,
        notification_read_at=fire.notification_read_at,
        error_code=fire.error_code, error_detail=fire.error_detail,
        created_at=fire.created_at, updated_at=fire.updated_at,
    )


def _shape(body: AgentScheduleCreate | AgentSchedulePatch, existing=None) -> dict:
    values = {
        "schedule_type": getattr(body, "schedule_type", None) or existing.schedule_type,
        "run_at": getattr(body, "run_at", None),
        "cron_expression": getattr(body, "cron_expression", None),
        "interval_seconds": getattr(body, "interval_seconds", None),
        "timezone": getattr(body, "timezone", None) or existing.timezone,
    }
    if existing is not None:
        fields = body.model_fields_set
        for name in ("run_at", "cron_expression", "interval_seconds"):
            if name not in fields:
                values[name] = getattr(existing, name)
    if values["schedule_type"] == "cron" and values["cron_expression"]:
        values["cron_expression"] = validate_cron_expression(values["cron_expression"])
    valid_shape = {
        "once": values["run_at"] is not None and values["cron_expression"] is None and values["interval_seconds"] is None,
        "cron": values["run_at"] is None and values["cron_expression"] is not None and values["interval_seconds"] is None,
        "interval": values["run_at"] is None and values["cron_expression"] is None and values["interval_seconds"] is not None,
    }[values["schedule_type"]]
    if not valid_shape:
        raise ValueError("schedule fields do not match schedule_type")
    zone(values["timezone"])
    return values


@router.get("", response_model=list[AgentScheduleResponse])
async def list_schedules(
    include_disabled: bool = False, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    statement = select(AgentSchedule).where(
        AgentSchedule.tenant_id == current.tenant_id,
        AgentSchedule.account_id == current.account.id,
    )
    if not include_disabled:
        statement = statement.where(AgentSchedule.status != "disabled")
    rows = list(await db.scalars(statement.order_by(
        AgentSchedule.updated_at.desc(), AgentSchedule.id.desc(),
    )))
    return [await _schedule_view(db, item) for item in rows]


@router.post("", response_model=AgentScheduleResponse, status_code=201)
async def create_schedule(
    body: AgentScheduleCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    record, replay = await _idempotency(
        db, current=current, operation="create_agent_schedule", key=idempotency_key,
        request=body.model_dump(mode="json"),
    )
    if replay and record.resource_id:
        return await _schedule_view(
            db, await _owned_schedule(db, record.resource_id, current)
        )
    agent = await _visible_agent(db, body.agent_id, current)
    now = utcnow()
    try:
        values = _shape(body)
        next_at = initial_fire_at(
            schedule_type=values["schedule_type"],
            timezone_name=values["timezone"], now=now,
            run_at=values["run_at"], cron_expression=values["cron_expression"],
            interval_seconds=values["interval_seconds"],
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail={"code": "invalid_schedule", "message": str(error)})
    end_at = utc_naive(body.end_at) if body.end_at else None
    if end_at is not None and end_at < next_at:
        raise HTTPException(status_code=422, detail={"code": "end_before_first_fire"})
    pinned_id = body.pinned_agent_version_id
    if body.version_policy == "pinned":
        pinned_id = pinned_id or agent.current_published_version_id
        version = await db.get(AgentAssemblyVersion, pinned_id)
        if (
            version is None or version.agent_id != agent.id
            or version.publication_status != "published"
        ):
            raise HTTPException(status_code=422, detail={"code": "invalid_pinned_version"})
    item = AgentSchedule(
        id=uuid7(), tenant_id=current.tenant_id, account_id=current.account.id,
        user_id=current.account.user_id, agent_id=agent.id,
        name=body.name, prompt=body.prompt,
        schedule_type=values["schedule_type"], run_at=(
            utc_naive(values["run_at"]) if values["run_at"] else None
        ), cron_expression=values["cron_expression"],
        interval_seconds=values["interval_seconds"], timezone=values["timezone"],
        next_fire_at=next_at, end_at=end_at,
        version_policy=body.version_policy, pinned_agent_version_id=pinned_id,
        overlap_policy=body.overlap_policy, misfire_policy=body.misfire_policy,
        misfire_grace_seconds=body.misfire_grace_seconds,
        status="active", total_fire_count=0,
    )
    db.add(item)
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="configuration", action="schedule.created",
        actor_type="account", actor_id=current.account.id,
        actor_user_id=current.account.user_id,
        resource_type="agent_schedule", resource_id=item.id,
        resource_version=item.version, outcome="succeeded",
        summary="Agent schedule created",
        dedupe_key=f"schedule:create:{record.id}", tenant_id=current.tenant_id,
        changes={"schedule_type": item.schedule_type, "agent_id": item.agent_id},
    )
    record.resource_type, record.resource_id, record.http_status = "agent_schedule", item.id, 201
    record.response_snapshot = {"id": item.id}
    await db.commit()
    return await _schedule_view(db, item)


@router.get("/{schedule_id}", response_model=AgentScheduleResponse)
async def get_schedule(
    schedule_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    return await _schedule_view(db, await _owned_schedule(db, schedule_id, current))


@router.patch("/{schedule_id}", response_model=AgentScheduleResponse)
async def update_schedule(
    schedule_id: str, body: AgentSchedulePatch,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    item = await _owned_schedule(db, schedule_id, current, lock=True)
    if item.status == "disabled":
        raise HTTPException(status_code=409, detail={"code": "schedule_disabled"})
    if item.version != _if_match(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": item.version})
    changes = body.model_dump(exclude_unset=True)
    timing_fields = {"schedule_type", "run_at", "cron_expression", "interval_seconds", "timezone"}
    timing_changed = bool(timing_fields.intersection(body.model_fields_set))
    if timing_changed:
        try:
            values = _shape(body, item)
            item.next_fire_at = initial_fire_at(
                schedule_type=values["schedule_type"],
                timezone_name=values["timezone"], now=utcnow(),
                run_at=values["run_at"],
                cron_expression=values["cron_expression"],
                interval_seconds=values["interval_seconds"],
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code": "invalid_schedule", "message": str(error)})
        item.schedule_type = values["schedule_type"]
        item.run_at = utc_naive(values["run_at"]) if values["run_at"] else None
        item.cron_expression = values["cron_expression"]
        item.interval_seconds = values["interval_seconds"]
        item.timezone = values["timezone"]
        if item.status == "completed":
            item.status = "paused"
    for name in ("name", "prompt", "overlap_policy", "misfire_policy", "misfire_grace_seconds"):
        if name in body.model_fields_set:
            value = getattr(body, name)
            if value is None:
                raise HTTPException(status_code=422, detail={"code": f"{name}_required"})
            setattr(item, name, value)
    if "end_at" in body.model_fields_set:
        item.end_at = utc_naive(body.end_at) if body.end_at else None
    if "version_policy" in body.model_fields_set or "pinned_agent_version_id" in body.model_fields_set:
        policy = body.version_policy or item.version_policy
        pinned = body.pinned_agent_version_id if "pinned_agent_version_id" in body.model_fields_set else item.pinned_agent_version_id
        agent = await _visible_agent(db, item.agent_id, current)
        if policy == "pinned":
            pinned = pinned or agent.current_published_version_id
            version = await db.get(AgentAssemblyVersion, pinned)
            if version is None or version.agent_id != agent.id or version.publication_status != "published":
                raise HTTPException(status_code=422, detail={"code": "invalid_pinned_version"})
        else:
            pinned = None
        item.version_policy, item.pinned_agent_version_id = policy, pinned
    if item.end_at and item.next_fire_at and item.end_at < item.next_fire_at:
        raise HTTPException(status_code=422, detail={"code": "end_before_next_fire"})
    old_version = item.version
    item.version += 1
    await PlatformAuditWriter(db).append(
        category="configuration", action="schedule.updated",
        actor_type="account", actor_id=current.account.id,
        actor_user_id=current.account.user_id,
        resource_type="agent_schedule", resource_id=item.id,
        resource_version=item.version, outcome="succeeded",
        summary="Agent schedule updated",
        dedupe_key=f"schedule:update:{item.id}:{item.version}", tenant_id=current.tenant_id,
        changes={"from_version": old_version, "fields": sorted(changes)},
    )
    await db.commit()
    return await _schedule_view(db, item)


@router.post("/{schedule_id}/pause", response_model=AgentScheduleResponse)
async def pause_schedule(
    schedule_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    item = await _owned_schedule(db, schedule_id, current, lock=True)
    if item.version != _if_match(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": item.version})
    if item.status == "disabled":
        raise HTTPException(status_code=409, detail={"code": "schedule_disabled"})
    if item.status != "paused":
        item.status = "paused"
        item.lease_owner = item.lease_token = item.lease_expires_at = None
        item.version += 1
        await cancel_pending_fires(db, item.id, reason="schedule_paused")
        await PlatformAuditWriter(db).append(
            category="configuration", action="schedule.paused",
            actor_type="account", actor_id=current.account.id,
            actor_user_id=current.account.user_id, resource_type="agent_schedule",
            resource_id=item.id, resource_version=item.version, outcome="succeeded",
            summary="Agent schedule paused",
            dedupe_key=f"schedule:pause:{item.id}:{item.version}", tenant_id=current.tenant_id,
        )
    await db.commit()
    return await _schedule_view(db, item)


@router.post("/{schedule_id}/resume", response_model=AgentScheduleResponse)
async def resume_schedule(
    schedule_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    item = await _owned_schedule(db, schedule_id, current, lock=True)
    if item.version != _if_match(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": item.version})
    if item.status == "disabled":
        raise HTTPException(status_code=409, detail={"code": "schedule_disabled"})
    await _visible_agent(db, item.agent_id, current)
    try:
        item.next_fire_at = initial_fire_at(
            schedule_type=item.schedule_type, timezone_name=item.timezone,
            now=utcnow(), run_at=item.run_at,
            cron_expression=item.cron_expression,
            interval_seconds=item.interval_seconds,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail={"code": "schedule_cannot_resume", "message": str(error)})
    if item.end_at and item.next_fire_at > item.end_at:
        raise HTTPException(status_code=409, detail={"code": "schedule_ended"})
    item.status = "active"
    item.last_error_code = item.last_error_detail = None
    item.version += 1
    await PlatformAuditWriter(db).append(
        category="configuration", action="schedule.resumed",
        actor_type="account", actor_id=current.account.id,
        actor_user_id=current.account.user_id, resource_type="agent_schedule",
        resource_id=item.id, resource_version=item.version, outcome="succeeded",
        summary="Agent schedule resumed",
        dedupe_key=f"schedule:resume:{item.id}:{item.version}", tenant_id=current.tenant_id,
    )
    await db.commit()
    return await _schedule_view(db, item)


@router.post("/{schedule_id}/run-now", response_model=AgentScheduleFireResponse, status_code=202)
async def run_schedule_now(
    schedule_id: str,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    item = await _owned_schedule(db, schedule_id, current, lock=True)
    if item.status == "disabled":
        raise HTTPException(status_code=409, detail={"code": "schedule_disabled"})
    await _visible_agent(db, item.agent_id, current)
    record, replay = await _idempotency(
        db, current=current, operation="run_agent_schedule_now", key=idempotency_key,
        request={"schedule_id": schedule_id},
    )
    fire = await create_manual_fire(db, schedule=item, operation_key=f"manual:{record.id}")
    if not replay:
        record.resource_type, record.resource_id, record.http_status = "agent_schedule_fire", fire.id, 202
        record.response_snapshot = {"id": fire.id}
        await PlatformAuditWriter(db).append(
            category="execution", action="schedule.run_now_requested",
            actor_type="account", actor_id=current.account.id,
            actor_user_id=current.account.user_id,
            resource_type="agent_schedule_fire", resource_id=fire.id,
            outcome="succeeded", summary="Agent schedule run requested",
            dedupe_key=f"schedule:run-now:{record.id}", tenant_id=current.tenant_id,
            metadata={"schedule_id": item.id},
        )
    await db.commit()
    return await _fire_view(db, fire)


@router.delete("/{schedule_id}", status_code=204)
async def disable_schedule(
    schedule_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    item = await _owned_schedule(db, schedule_id, current, lock=True)
    if item.version != _if_match(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": item.version})
    if item.status != "disabled":
        item.status = "disabled"
        item.next_fire_at = None
        item.lease_owner = item.lease_token = item.lease_expires_at = None
        item.version += 1
        await cancel_pending_fires(db, item.id, reason="schedule_disabled")
        await PlatformAuditWriter(db).append(
            category="configuration", action="schedule.disabled",
            actor_type="account", actor_id=current.account.id,
            actor_user_id=current.account.user_id, resource_type="agent_schedule",
            resource_id=item.id, resource_version=item.version, outcome="succeeded",
            summary="Agent schedule disabled",
            dedupe_key=f"schedule:disable:{item.id}:{item.version}", tenant_id=current.tenant_id,
        )
    await db.commit()
    return Response(status_code=204)


@router.get("/{schedule_id}/fires", response_model=list[AgentScheduleFireResponse])
async def list_schedule_fires(
    schedule_id: str, limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    await _owned_schedule(db, schedule_id, current)
    limit = max(1, min(limit, 200))
    rows = list(await db.scalars(select(AgentScheduleFire).where(
        AgentScheduleFire.schedule_id == schedule_id,
    ).order_by(
        AgentScheduleFire.scheduled_for.desc(), AgentScheduleFire.id.desc(),
    ).limit(limit)))
    return [await _fire_view(db, item) for item in rows]


@router.get("/notifications/unread", response_model=list[AgentScheduleFireResponse])
async def unread_schedule_notifications(
    limit: int = 20, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    limit = max(1, min(limit, 100))
    rows = list(await db.scalars(select(AgentScheduleFire).where(
        AgentScheduleFire.tenant_id == current.tenant_id,
        AgentScheduleFire.account_id == current.account.id,
        or_(
            AgentScheduleFire.status.in_(("succeeded", "failed")),
            and_(
                AgentScheduleFire.status == "cancelled",
                AgentScheduleFire.run_id.is_not(None),
            ),
        ),
        AgentScheduleFire.notification_read_at.is_(None),
    ).order_by(
        AgentScheduleFire.completed_at.desc(), AgentScheduleFire.id.desc(),
    ).limit(limit)))
    return [await _fire_view(db, item) for item in rows]


@router.post("/notifications/{fire_id}/read", status_code=204)
async def read_schedule_notification(
    fire_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    fire = await db.scalar(select(AgentScheduleFire).where(
        AgentScheduleFire.id == fire_id,
        AgentScheduleFire.tenant_id == current.tenant_id,
        AgentScheduleFire.account_id == current.account.id,
    ).with_for_update())
    if fire is None:
        raise HTTPException(status_code=404, detail="Schedule fire not found")
    fire.notification_read_at = fire.notification_read_at or utcnow()
    await db.commit()
    return Response(status_code=204)


@router.post("/notifications/read-all", status_code=204)
async def read_all_schedule_notifications(
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    await db.execute(update(AgentScheduleFire).where(
        AgentScheduleFire.tenant_id == current.tenant_id,
        AgentScheduleFire.account_id == current.account.id,
        AgentScheduleFire.status.in_(("succeeded", "failed", "cancelled")),
        AgentScheduleFire.notification_read_at.is_(None),
    ).values(notification_read_at=utcnow()))
    await db.commit()
    return Response(status_code=204)
