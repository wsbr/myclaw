"""Resolve scheduled execution meaning from the existing ToolInvocation graph."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.models.runtime_v1 import DurableRunner, ToolInvocation
from app.models.schedule_v1 import AgentSchedule, AgentScheduleFire


DEFINITION_SNAPSHOT_KEY = "agent_schedule_definition_v1"


@dataclass(frozen=True)
class SchedulePrincipal:
    account_id: str
    user_id: str
    display_name: str
    handle: str
    role: str

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "handle": self.handle,
            "role": self.role,
        }


@dataclass(frozen=True)
class ScheduleDefinition:
    tool_invocation_id: str | None
    agent_id: str
    name: str
    instruction: str
    context_mode: str
    source_session_id: str | None
    version_policy: str
    pinned_agent_version_id: str | None
    principals: tuple[SchedulePrincipal, ...] = ()

    def as_dict(self) -> dict:
        return {
            "version": 1,
            "agent_id": self.agent_id,
            "name": self.name,
            "instruction": self.instruction,
            "context_mode": self.context_mode,
            "source_session_id": self.source_session_id,
            "version_policy": self.version_policy,
            "pinned_agent_version_id": self.pinned_agent_version_id,
            "principals": [item.as_dict() for item in self.principals],
        }

    @property
    def content_hash(self) -> str:
        encoded = json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def _from_snapshot(invocation_id: str, value: dict) -> ScheduleDefinition:
    principals = tuple(SchedulePrincipal(
        account_id=str(item["account_id"]),
        user_id=str(item["user_id"]),
        display_name=str(item["display_name"]),
        handle=str(item["handle"]),
        role=str(item["role"]),
    ) for item in value.get("principals") or [])
    return ScheduleDefinition(
        tool_invocation_id=invocation_id,
        agent_id=str(value["agent_id"]),
        name=str(value["name"]),
        instruction=str(value["instruction"]),
        context_mode=str(value.get("context_mode") or "new_session"),
        source_session_id=value.get("source_session_id"),
        version_policy=str(value.get("version_policy") or "latest_published"),
        pinned_agent_version_id=value.get("pinned_agent_version_id"),
        principals=principals,
    )


async def persist_schedule_definition(
    db, *, invocation_id: str, definition: ScheduleDefinition,
) -> ScheduleDefinition:
    invocation = await db.get(ToolInvocation, invocation_id)
    if invocation is None or invocation.tool_key != "schedule_task":
        raise ValueError("schedule definition ToolInvocation is unavailable")
    runner = await db.get(DurableRunner, invocation.runner_id)
    if runner is None or runner.agent_id != definition.agent_id:
        raise ValueError("schedule definition Agent does not match its ToolInvocation")
    normalized = ScheduleDefinition(
        tool_invocation_id=invocation.id,
        agent_id=definition.agent_id,
        name=definition.name,
        instruction=definition.instruction,
        context_mode=definition.context_mode,
        source_session_id=definition.source_session_id,
        version_policy=definition.version_policy,
        pinned_agent_version_id=definition.pinned_agent_version_id,
        principals=definition.principals,
    )
    invocation.authorization_snapshot = {
        **dict(invocation.authorization_snapshot or {}),
        DEFINITION_SNAPSHOT_KEY: normalized.as_dict(),
    }
    return normalized


async def load_schedule_definition(
    db, schedule: AgentSchedule, *, fire: AgentScheduleFire | None = None,
) -> ScheduleDefinition:
    invocation_id = (
        fire.definition_tool_invocation_id
        if fire is not None and fire.definition_tool_invocation_id
        else schedule.definition_tool_invocation_id
    )
    if invocation_id:
        invocation = await db.get(ToolInvocation, invocation_id)
        if invocation is not None and (
            invocation.tool_key != "schedule_task"
            or invocation.tenant_id != schedule.tenant_id
            or invocation.account_id != schedule.account_id
        ):
            raise ValueError("schedule definition boundary is invalid")
        runner = await db.get(DurableRunner, invocation.runner_id) if invocation else None
        snapshot = dict(invocation.authorization_snapshot or {}).get(
            DEFINITION_SNAPSHOT_KEY
        ) if invocation else None
        if isinstance(snapshot, dict):
            definition = _from_snapshot(invocation_id, snapshot)
            if runner is None or runner.agent_id != definition.agent_id:
                raise ValueError("schedule definition Agent boundary is invalid")
            return definition
        # Definitions created before 0049 still have immutable Tool arguments;
        # use the schedule's compatibility columns to complete their projection.
        if invocation is not None:
            arguments = dict(invocation.arguments or {})
            instruction = str(
                arguments.get("instruction") or schedule.legacy_prompt or ""
            ).strip()
            agent_id = runner.agent_id if runner else schedule.legacy_agent_id
            if instruction and agent_id:
                return ScheduleDefinition(
                    tool_invocation_id=invocation.id,
                    agent_id=agent_id,
                    name=str(arguments.get("name") or schedule.legacy_name or instruction[:80]),
                    instruction=instruction,
                    context_mode=str(
                        arguments.get("context_mode")
                        or schedule.legacy_context_mode or "new_session"
                    ),
                    source_session_id=(
                        schedule.legacy_source_session_id or invocation.session_id
                    ),
                    version_policy=str(
                        arguments.get("version_policy")
                        or schedule.legacy_version_policy or "latest_published"
                    ),
                    pinned_agent_version_id=schedule.legacy_pinned_agent_version_id,
                )
    if schedule.legacy_agent_id and schedule.legacy_prompt:
        return ScheduleDefinition(
            tool_invocation_id=None,
            agent_id=schedule.legacy_agent_id,
            name=schedule.legacy_name or schedule.legacy_prompt[:80],
            instruction=(
                fire.legacy_prompt_snapshot
                if fire is not None and fire.legacy_prompt_snapshot
                else schedule.legacy_prompt
            ),
            context_mode=(
                fire.legacy_context_mode_snapshot
                if fire is not None and fire.legacy_context_mode_snapshot
                else schedule.legacy_context_mode or "new_session"
            ),
            source_session_id=(
                fire.legacy_source_session_id_snapshot
                if fire is not None and fire.legacy_source_session_id_snapshot
                else schedule.legacy_source_session_id
            ),
            version_policy=(
                fire.legacy_version_policy_snapshot
                if fire is not None and fire.legacy_version_policy_snapshot
                else schedule.legacy_version_policy or "latest_published"
            ),
            pinned_agent_version_id=(
                fire.agent_version_id if fire is not None and fire.agent_version_id
                else schedule.legacy_pinned_agent_version_id
            ),
        )
    raise ValueError("schedule execution definition is unavailable")
