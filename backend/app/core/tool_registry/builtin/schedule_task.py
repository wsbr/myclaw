"""Model-facing, governed control surface for durable Agent schedules."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, PlatformAccountRole, UserSession
from app.models.runtime_v1 import DurableRunner
from app.models.schedule_v1 import AgentSchedule
from app.models.tenant_v1 import TenantMembership
from app.persistence.reliable_writes import PlatformAuditWriter
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


MUTATING_OPERATIONS = frozenset(
    {"create", "update", "pause", "resume", "delete", "run_now"}
)
# Keep the fallback aligned with runtime_facts.PLATFORM_TIMEZONE without
# importing the runtime package here (its public __init__ imports ToolRegistry).
DEFAULT_PLATFORM_TIMEZONE = "Asia/Shanghai"


def _failure(
    code: str, message: str, *, category: str = "validation",
    details: dict | None = None,
) -> ToolResult:
    return ToolResult(
        status="failed", summary=message, error=message, error_code=code,
        error_category=category, error_details=details or {}, retryable=False,
    )


def _parse_offset_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an RFC 3339 datetime with a UTC offset")
    rendered = value.strip()
    if rendered.endswith("Z"):
        rendered = f"{rendered[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as error:
        raise ValueError(f"{field} must be an RFC 3339 datetime with a UTC offset") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed


def _schedule_data(item: AgentSchedule) -> dict:
    return {
        "id": item.id,
        "agent_id": item.agent_id,
        "name": item.name,
        "instruction": item.prompt,
        "context_mode": item.context_mode,
        "schedule_type": item.schedule_type,
        "cron_expression": item.cron_expression,
        "interval_seconds": item.interval_seconds,
        "run_at": item.run_at.isoformat() if item.run_at else None,
        "timezone": item.timezone,
        "next_fire_at": item.next_fire_at.isoformat() if item.next_fire_at else None,
        "next_fire_local": (
            local_iso(item.next_fire_at, item.timezone)
            if item.next_fire_at else None
        ),
        "end_at": item.end_at.isoformat() if item.end_at else None,
        "version_policy": item.version_policy,
        "overlap_policy": item.overlap_policy,
        "misfire_policy": item.misfire_policy,
        "status": item.status,
        "total_fire_count": item.total_fire_count,
        "version": item.version,
    }


async def _authority(db, context: RuntimeContext) -> ToolResult | tuple:
    if db is None:
        return _failure(
            "schedule_database_required", "A durable database context is required",
            category="transient",
        )
    if not context.account_id:
        return _failure(
            "schedule_account_required", "An authenticated platform account is required",
            category="authorization",
        )
    account = await db.get(PlatformAccount, context.account_id)
    membership = await db.scalar(select(TenantMembership).where(
        TenantMembership.tenant_id == context.tenant_id,
        TenantMembership.account_id == context.account_id,
        TenantMembership.status == "active",
    ))
    session = await db.scalar(select(UserSession).where(
        UserSession.id == context.session_id,
        UserSession.tenant_id == context.tenant_id,
        UserSession.account_id == context.account_id,
        UserSession.user_id == context.user_id,
        UserSession.status == "active",
    ))
    agent = await db.scalar(select(AgentBlueprint).where(
        AgentBlueprint.id == context.agent_id,
        AgentBlueprint.tenant_id == context.tenant_id,
        AgentBlueprint.status == "published",
    ))
    if (
        account is None or account.status != "active"
        or account.tenant_id != context.tenant_id
        or account.user_id != context.user_id
        or membership is None or session is None
        or agent is None or not agent.current_published_version_id
    ):
        return _failure(
            "schedule_authority_unavailable",
            "The current account, Session, or Agent is no longer authorized",
            category="authorization",
        )
    roles = set(await db.scalars(select(PlatformAccountRole.role_key).where(
        PlatformAccountRole.account_id == account.id,
    )))
    if not await has_agent_use_access(
        db, agent=agent, account_id=account.id, roles=roles,
    ):
        return _failure(
            "schedule_agent_not_visible", "The current Agent is not visible",
            category="authorization",
        )
    return account, session, agent


async def _owned_schedule(
    db, context: RuntimeContext, schedule_id: object,
) -> AgentSchedule | ToolResult:
    if not isinstance(schedule_id, str) or not schedule_id.strip():
        return _failure("schedule_id_required", "schedule_id is required")
    item = await db.scalar(select(AgentSchedule).where(
        AgentSchedule.id == schedule_id.strip(),
        AgentSchedule.tenant_id == context.tenant_id,
        AgentSchedule.account_id == context.account_id,
        AgentSchedule.user_id == context.user_id,
        AgentSchedule.agent_id == context.agent_id,
    ).with_for_update())
    if item is None:
        return _failure(
            "schedule_not_found",
            "No schedule owned by this user and current Agent has that ID",
            category="authorization",
        )
    return item


def _trigger_values(trigger: object, *, now: datetime, default_timezone: str) -> dict:
    if not isinstance(trigger, dict):
        raise ValueError("trigger is required")
    trigger_type = trigger.get("type")
    timezone_name = str(trigger.get("timezone") or default_timezone).strip()
    zone(timezone_name)
    run_at = None
    cron_expression = None
    interval_seconds = None
    if trigger_type == "delay":
        delay = trigger.get("delay_seconds")
        if not isinstance(delay, int) or isinstance(delay, bool) or delay < 60:
            raise ValueError("delay_seconds must be an integer of at least 60")
        run_at = now + timedelta(seconds=delay)
        schedule_type = "once"
    elif trigger_type == "once":
        run_at = _parse_offset_datetime(trigger.get("run_at"), field="trigger.run_at")
        schedule_type = "once"
    elif trigger_type == "cron":
        cron_expression = validate_cron_expression(
            str(trigger.get("cron_expression") or "")
        )
        schedule_type = "cron"
    elif trigger_type == "interval":
        interval_seconds = trigger.get("interval_seconds")
        if (
            not isinstance(interval_seconds, int)
            or isinstance(interval_seconds, bool)
            or interval_seconds < 60
            or interval_seconds > 31_536_000
        ):
            raise ValueError("interval_seconds must be between 60 and 31536000")
        schedule_type = "interval"
    else:
        raise ValueError("trigger.type must be delay, once, cron, or interval")
    next_fire_at = initial_fire_at(
        schedule_type=schedule_type, timezone_name=timezone_name, now=now,
        run_at=run_at, cron_expression=cron_expression,
        interval_seconds=interval_seconds,
    )
    return {
        "schedule_type": schedule_type,
        "timezone": timezone_name,
        "run_at": utc_naive(run_at) if run_at else None,
        "cron_expression": cron_expression,
        "interval_seconds": interval_seconds,
        "next_fire_at": next_fire_at,
    }


class ScheduleTaskTool(BaseTool):
    name = "schedule_task"
    description = (
        "WHEN: the user explicitly asks this Agent to remind them, do work later, repeat work, "
        "or list/manage an existing schedule. Natural language intent must be converted into an "
        "explicit trigger; use delay_seconds for relative delays. Choose continue_session only "
        "when later work needs this conversation's materials, otherwise use new_session. "
        "WHEN NOT: do not call for work that should happen now, never guess an ambiguous local "
        "clock time or timezone, and never target another Agent. Ask the user when timing is "
        "ambiguous. RETURNS: the persisted schedule, bounded owned schedule list, or manual fire. "
        "RECOVERY: correct the trigger from the authoritative runtime clock, list schedules to "
        "obtain an ID, or ask the user to clarify the exact time and recurrence."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["create", "list", "update", "pause", "resume", "delete", "run_now"],
            },
            "schedule_id": {"type": "string", "minLength": 1, "maxLength": 36},
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "instruction": {"type": "string", "minLength": 1, "maxLength": 16000},
            "context_mode": {
                "type": "string", "enum": ["new_session", "continue_session"],
                "default": "new_session",
            },
            "trigger": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["delay", "once", "cron", "interval"]},
                    "delay_seconds": {"type": "integer", "minimum": 60, "maximum": 31_536_000},
                    "run_at": {"type": "string", "format": "date-time"},
                    "cron_expression": {"type": "string", "minLength": 1, "maxLength": 191},
                    "interval_seconds": {"type": "integer", "minimum": 60, "maximum": 31_536_000},
                    "timezone": {"type": "string", "minLength": 1, "maxLength": 100},
                },
                "required": ["type"],
                "additionalProperties": False,
            },
            "version_policy": {
                "type": "string", "enum": ["latest_published", "pinned"],
                "default": "latest_published",
            },
            "overlap_policy": {
                "type": "string", "enum": ["skip", "queue_one", "allow"],
                "default": "skip",
            },
            "misfire_policy": {
                "type": "string", "enum": ["skip", "fire_once"],
                "default": "fire_once",
            },
            "misfire_grace_seconds": {
                "type": "integer", "minimum": 0, "maximum": 86400,
                "default": 300,
            },
            "end_at": {"type": ["string", "null"], "format": "date-time"},
            "status": {
                "type": "string",
                "enum": ["all", "active", "paused", "completed"],
                "default": "all",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        },
        "required": ["operation"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        operation = args["operation"]
        authority = await _authority(db, context)
        if isinstance(authority, ToolResult):
            return authority
        account, source_session, agent = authority
        if operation in MUTATING_OPERATIONS:
            if context.authentication_source == "schedule":
                return _failure(
                    "schedule_recursive_mutation_denied",
                    "A schedule-triggered Run cannot create or mutate schedules",
                    category="authorization",
                )
            if not context.tool_invocation_id:
                return _failure(
                    "schedule_invocation_required",
                    "A durable Tool invocation is required for schedule mutation",
                    category="authorization",
                )

        if operation == "list":
            statement = select(AgentSchedule).where(
                AgentSchedule.tenant_id == context.tenant_id,
                AgentSchedule.account_id == account.id,
                AgentSchedule.user_id == context.user_id,
                AgentSchedule.agent_id == context.agent_id,
                AgentSchedule.status != "disabled",
            )
            status = args.get("status", "all")
            if status != "all":
                statement = statement.where(AgentSchedule.status == status)
            limit = int(args.get("limit", 20))
            items = list(await db.scalars(statement.order_by(
                AgentSchedule.updated_at.desc(), AgentSchedule.id.desc(),
            ).limit(limit)))
            data = {
                "operation": operation,
                "items": [_schedule_data(item) for item in items],
                "count": len(items),
            }
            return ToolResult(
                status="succeeded", summary=f"Found {len(items)} schedules for this Agent",
                data=data, content_blocks=[{"type": "json", "value": data}],
            )

        if operation == "create":
            existing = await db.scalar(select(AgentSchedule).where(
                AgentSchedule.created_by_tool_invocation_id == context.tool_invocation_id,
            ))
            if existing is not None:
                data = {"operation": operation, "schedule": _schedule_data(existing), "replayed": True}
                return ToolResult(
                    status="succeeded", summary=f"Schedule already exists: {existing.name}",
                    data=data, content_blocks=[{"type": "json", "value": data}],
                )
            instruction = str(args.get("instruction") or "").strip()
            if not instruction:
                return _failure("schedule_instruction_required", "instruction is required for create")
            now = utcnow()
            default_timezone = str(
                context.runtime_clock.get("timezone") or DEFAULT_PLATFORM_TIMEZONE
            )
            try:
                timing = _trigger_values(
                    args.get("trigger"), now=now, default_timezone=default_timezone,
                )
                end_at = (
                    utc_naive(_parse_offset_datetime(args.get("end_at"), field="end_at"))
                    if args.get("end_at") else None
                )
            except ValueError as error:
                return _failure("invalid_schedule_trigger", str(error))
            if end_at is not None and end_at < timing["next_fire_at"]:
                return _failure("schedule_end_before_first_fire", "end_at is before the first fire")
            context_mode = args.get("context_mode", "new_session")
            if (
                context_mode == "continue_session"
                and source_session.entry_agent_id != context.agent_id
            ):
                return _failure(
                    "schedule_continuation_requires_entry_agent",
                    "continue_session is only available to the conversation's entry Agent; use new_session",
                    category="conflict",
                )
            version_policy = args.get("version_policy", "latest_published")
            pinned_id = None
            if version_policy == "pinned":
                runner = await db.get(DurableRunner, context.runner_id)
                version = (
                    await db.get(AgentAssemblyVersion, runner.agent_version_id)
                    if runner else None
                )
                if (
                    version is None or version.agent_id != agent.id
                    or version.publication_status != "published"
                ):
                    return _failure(
                        "schedule_pinned_version_unavailable",
                        "The current Runner does not use a published version that can be pinned",
                        category="conflict",
                    )
                pinned_id = version.id
            item = AgentSchedule(
                id=uuid7(), tenant_id=context.tenant_id, account_id=account.id,
                user_id=context.user_id, agent_id=context.agent_id,
                name=str(args.get("name") or instruction[:80]).strip(),
                prompt=instruction, context_mode=context_mode,
                source_session_id=source_session.id,
                created_by_tool_invocation_id=context.tool_invocation_id,
                schedule_type=timing["schedule_type"], run_at=timing["run_at"],
                cron_expression=timing["cron_expression"],
                interval_seconds=timing["interval_seconds"],
                timezone=timing["timezone"], next_fire_at=timing["next_fire_at"],
                end_at=end_at, version_policy=version_policy,
                pinned_agent_version_id=pinned_id,
                overlap_policy=args.get("overlap_policy", "skip"),
                misfire_policy=args.get("misfire_policy", "fire_once"),
                misfire_grace_seconds=int(args.get("misfire_grace_seconds", 300)),
                status="active", total_fire_count=0,
            )
            db.add(item)
            await db.flush()
            await self._audit(
                db, context, item, action="schedule.created_by_agent",
                changes={
                    "schedule_type": item.schedule_type,
                    "context_mode": item.context_mode,
                    "agent_id": item.agent_id,
                },
            )
            data = {"operation": operation, "schedule": _schedule_data(item), "replayed": False}
            return ToolResult(
                status="succeeded", summary=f"Schedule created: {item.name}",
                data=data, content_blocks=[{"type": "json", "value": data}],
                evidence=[{
                    "kind": "agent_schedule", "schedule_id": item.id,
                    "tool_invocation_id": context.tool_invocation_id,
                    "source_session_id": source_session.id,
                }],
            )

        item = await _owned_schedule(db, context, args.get("schedule_id"))
        if isinstance(item, ToolResult):
            return item
        if item.status == "disabled":
            return _failure(
                "schedule_disabled", "The schedule has been disabled",
                category="conflict",
            )

        if operation == "update":
            changed: list[str] = []
            if "name" in args:
                name = str(args["name"]).strip()
                if not name:
                    return _failure("schedule_name_required", "name must not be blank")
                item.name = name
                changed.append("name")
            if "instruction" in args:
                instruction = str(args["instruction"]).strip()
                if not instruction:
                    return _failure("schedule_instruction_required", "instruction must not be blank")
                item.prompt = instruction
                changed.append("instruction")
            if "trigger" in args:
                try:
                    timing = _trigger_values(
                        args["trigger"], now=utcnow(),
                        default_timezone=item.timezone,
                    )
                except ValueError as error:
                    return _failure("invalid_schedule_trigger", str(error))
                for key in (
                    "schedule_type", "run_at", "cron_expression",
                    "interval_seconds", "timezone", "next_fire_at",
                ):
                    setattr(item, key, timing[key])
                changed.append("trigger")
                if item.status == "completed":
                    item.status = "paused"
            if "context_mode" in args:
                if (
                    args["context_mode"] == "continue_session"
                    and source_session.entry_agent_id != context.agent_id
                ):
                    return _failure(
                        "schedule_continuation_requires_entry_agent",
                        "continue_session is only available to the conversation's entry Agent; use new_session",
                        category="conflict",
                    )
                if args["context_mode"] == "continue_session" and not item.source_session_id:
                    return _failure(
                        "schedule_source_session_missing",
                        "This schedule was not created from a conversation and cannot continue one",
                        category="conflict",
                    )
                item.context_mode = args["context_mode"]
                changed.append("context_mode")
            for field in (
                "overlap_policy", "misfire_policy", "misfire_grace_seconds",
            ):
                if field in args:
                    setattr(item, field, args[field])
                    changed.append(field)
            if "end_at" in args:
                try:
                    item.end_at = (
                        utc_naive(_parse_offset_datetime(args["end_at"], field="end_at"))
                        if args["end_at"] else None
                    )
                except ValueError as error:
                    return _failure("invalid_schedule_end", str(error))
                changed.append("end_at")
            if "version_policy" in args:
                policy = args["version_policy"]
                if policy == "pinned":
                    runner = await db.get(DurableRunner, context.runner_id)
                    version = (
                        await db.get(AgentAssemblyVersion, runner.agent_version_id)
                        if runner else None
                    )
                    if (
                        version is None or version.agent_id != agent.id
                        or version.publication_status != "published"
                    ):
                        return _failure(
                            "schedule_pinned_version_unavailable",
                            "The current Runner version cannot be pinned",
                            category="conflict",
                        )
                    item.pinned_agent_version_id = version.id
                else:
                    item.pinned_agent_version_id = None
                item.version_policy = policy
                changed.append("version_policy")
            if not changed:
                return _failure("schedule_update_empty", "No schedule fields were supplied for update")
            if item.end_at and item.next_fire_at and item.end_at < item.next_fire_at:
                return _failure("schedule_end_before_next_fire", "end_at is before the next fire")
            item.version += 1
            await self._audit(
                db, context, item, action="schedule.updated_by_agent",
                changes={"fields": changed},
            )
            summary = f"Schedule updated: {item.name}"
            data = {"operation": operation, "schedule": _schedule_data(item)}
        elif operation == "pause":
            if item.status != "paused":
                item.status = "paused"
                item.lease_owner = item.lease_token = item.lease_expires_at = None
                item.version += 1
                await cancel_pending_fires(db, item.id, reason="schedule_paused_by_agent")
                await self._audit(db, context, item, action="schedule.paused_by_agent")
            summary = f"Schedule paused: {item.name}"
            data = {"operation": operation, "schedule": _schedule_data(item)}
        elif operation == "resume":
            if item.status != "active":
                try:
                    item.next_fire_at = initial_fire_at(
                        schedule_type=item.schedule_type, timezone_name=item.timezone,
                        now=utcnow(), run_at=item.run_at,
                        cron_expression=item.cron_expression,
                        interval_seconds=item.interval_seconds,
                    )
                except ValueError as error:
                    return _failure(
                        "schedule_cannot_resume", str(error), category="conflict",
                    )
                if item.end_at and item.next_fire_at > item.end_at:
                    return _failure("schedule_ended", "The schedule end time has passed", category="conflict")
                item.status = "active"
                item.last_error_code = item.last_error_detail = None
                item.version += 1
                await self._audit(db, context, item, action="schedule.resumed_by_agent")
            summary = f"Schedule resumed: {item.name}"
            data = {"operation": operation, "schedule": _schedule_data(item)}
        elif operation == "delete":
            item.status = "disabled"
            item.next_fire_at = None
            item.lease_owner = item.lease_token = item.lease_expires_at = None
            item.version += 1
            await cancel_pending_fires(db, item.id, reason="schedule_disabled_by_agent")
            await self._audit(db, context, item, action="schedule.disabled_by_agent")
            summary = f"Schedule deleted: {item.name}"
            data = {"operation": operation, "schedule_id": item.id, "status": item.status}
        elif operation == "run_now":
            fire = await create_manual_fire(
                db, schedule=item,
                operation_key=f"tool:{context.tool_invocation_id}",
            )
            await self._audit(
                db, context, item, action="schedule.run_now_requested_by_agent",
                resource_type="agent_schedule_fire", resource_id=fire.id,
            )
            summary = f"Schedule queued to run now: {item.name}"
            data = {
                "operation": operation,
                "schedule": _schedule_data(item),
                "fire": {"id": fire.id, "status": fire.status},
            }
        else:
            return _failure("schedule_operation_unsupported", "Unsupported schedule operation")
        return ToolResult(
            status="succeeded", summary=summary, data=data,
            content_blocks=[{"type": "json", "value": data}],
            evidence=[{
                "kind": "agent_schedule", "schedule_id": item.id,
                "tool_invocation_id": context.tool_invocation_id,
            }],
        )

    @staticmethod
    async def _audit(
        db, context: RuntimeContext, item: AgentSchedule, *, action: str,
        changes: dict | None = None, resource_type: str = "agent_schedule",
        resource_id: str | None = None,
    ) -> None:
        await PlatformAuditWriter(db).append(
            category="execution" if "run_now" in action else "configuration",
            action=action, actor_type="agent", actor_id=context.agent_id,
            actor_user_id=context.user_id, resource_type=resource_type,
            resource_id=resource_id or item.id,
            resource_version=item.version if resource_type == "agent_schedule" else None,
            outcome="succeeded", summary=action.replace("_", " "),
            dedupe_key=f"schedule:tool:{context.tool_invocation_id}:{action}",
            tenant_id=context.tenant_id, changes=changes,
            metadata={
                "account_id": context.account_id,
                "source_session_id": context.session_id,
                "tool_invocation_id": context.tool_invocation_id,
            },
        )
