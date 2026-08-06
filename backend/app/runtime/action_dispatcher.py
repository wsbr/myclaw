"""Capability, schema, persistence, and execution boundary for native Tool Calls."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tool_registry.base import RuntimeContext, ToolError, ToolResult
from app.core.tool_registry.registry import ToolRegistry
from app.models.common import utcnow, uuid7
from app.models.foundation import UserSession
from app.models.runtime_v1 import Approval, CapabilitySnapshot, SessionGrant, ToolInvocation
from app.persistence.reliable_writes import ExecutionEventWriter
from app.runtime.coordination_tools import dispatch_coordination_tool
from app.runtime.protocol import ToolCallItem


@dataclass(frozen=True)
class DispatchResult:
    call_id: str
    tool_name: str
    result: ToolResult
    invocation_id: str | None = None
    durable_wait: bool = False
    schema_error: bool = False
    wait_reason: str | None = None

    def provider_message(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_use_id": self.call_id,
            "tool_call_id": self.call_id,
            "content": json.dumps(self.result.model_dump(mode="json"), ensure_ascii=False),
        }


Executor = Callable[[str, dict[str, Any], RuntimeContext, AsyncSession | None], Awaitable[ToolResult]]


def _schema_errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return [f"{path} must be an object"]
        required = schema.get("required") or []
        errors.extend(f"{path}.{key} is required" for key in required if key not in value)
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            errors.extend(f"{path}.{key} is not allowed" for key in value if key not in properties)
        for key, child in properties.items():
            if key in value:
                errors.extend(_schema_errors(value[key], child, f"{path}.{key}"))
    elif expected == "array":
        if not isinstance(value, list):
            return [f"{path} must be an array"]
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path} has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path} has too many items")
        for index, item in enumerate(value):
            errors.extend(_schema_errors(item, schema.get("items") or {}, f"{path}[{index}]"))
    elif expected == "string" and not isinstance(value, str):
        errors.append(f"{path} must be a string")
    elif expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        errors.append(f"{path} must be an integer")
    elif expected == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        errors.append(f"{path} must be a number")
    elif expected == "boolean" and not isinstance(value, bool):
        errors.append(f"{path} must be a boolean")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path} is too long")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} is above maximum")
    return errors


async def _builtin_executor(name, arguments, context, db):
    return await ToolRegistry.execute(name, arguments, context, db=db)


class ActionDispatcher:
    def __init__(
        self, tool_definitions: list[dict[str, Any]], *,
        executor: Executor = _builtin_executor,
    ) -> None:
        self.definitions = {value["name"]: value for value in tool_definitions}
        self.executor = executor

    async def dispatch(
        self,
        call: ToolCallItem,
        *,
        capability_snapshot: CapabilitySnapshot | dict[str, Any],
        runtime_context: RuntimeContext,
        db: AsyncSession | None = None,
        run_id: str | None = None,
        work_item_id: str | None = None,
        source_turn_id: str | None = None,
    ) -> DispatchResult:
        effective = (
            capability_snapshot.effective_tool_keys
            if isinstance(capability_snapshot, CapabilitySnapshot)
            else capability_snapshot.get("effective_tool_keys", [])
        )
        definition = self.definitions.get(call.name)
        if call.name not in effective or definition is None:
            return DispatchResult(
                call.call_id, call.name,
                ToolResult(
                    status="failed", summary="Tool is not authorized",
                    error=ToolError(
                        code="tool_not_in_capability_snapshot",
                        message=f"Tool '{call.name}' is not available in this Runner",
                        category="authorization", details={},
                    ),
                ),
            )
        errors = _schema_errors(call.arguments, definition.get("input_schema") or {})
        if errors:
            return DispatchResult(
                call.call_id, call.name,
                ToolResult(
                    status="failed", summary="Tool arguments failed schema validation",
                    error=ToolError(
                        code="tool_arguments_schema_error", message="; ".join(errors),
                        category="validation", details={"errors": errors},
                    ),
                ), schema_error=True,
            )

        # CapabilitySnapshot is the first authorization layer.  Re-evaluate
        # the actual semantic side-effect class at the Dispatcher boundary so
        # a stale, forged, or incorrectly assembled snapshot still cannot turn
        # plan_readonly into a successful mutation.
        if (
            runtime_context.permission_mode == "plan_readonly"
            and definition.get("side_effect", "none") != "none"
        ):
            return DispatchResult(
                call.call_id, call.name,
                ToolResult(
                    status="failed",
                    summary="Plan read-only mode denied this side effect",
                    error=ToolError(
                        code="permission_mode_denied",
                        message=(
                            f"Tool '{call.name}' has side effects and cannot run "
                            "before the current Plan revision is approved"
                        ),
                        category="authorization",
                        details={"permission_mode": "plan_readonly"},
                    ),
                ),
            )

        if db is not None and run_id and work_item_id and source_turn_id:
            # Agent turns run concurrently, but every durable dispatch writes
            # a Session-scoped event sequence. Acquire that aggregate lock
            # before Tool/Group rows so all dispatch paths share one lock
            # order and cannot deadlock against settlement or Sandbox workers.
            await db.scalar(select(UserSession).where(
                UserSession.id == runtime_context.session_id,
            ).execution_options(populate_existing=True).with_for_update())

        if definition.get("source") == "runtime":
            if db is None or not all((run_id, work_item_id, source_turn_id)):
                return DispatchResult(
                    call.call_id, call.name,
                    ToolResult(
                        status="failed", summary="Durable Runtime context is required",
                        error_code="runtime_context_required",
                        error="Durable Runtime context is required",
                        error_category="permanent",
                    ),
                )
            result, durable_wait, wait_reason = await dispatch_coordination_tool(
                call.name, call.arguments, context=runtime_context, db=db,
                run_id=run_id, work_item_id=work_item_id,
                source_turn_id=source_turn_id, provider_call_id=call.call_id,
            )
            return DispatchResult(
                call.call_id, call.name, result,
                durable_wait=durable_wait, wait_reason=wait_reason,
            )

        invocation = None
        if db is not None and run_id and work_item_id and source_turn_id:
            encoded = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            arguments_hash = hashlib.sha256(encoded.encode()).hexdigest()
            operation_key = f"turn:{source_turn_id}:call:{call.call_id}"
            invocation = ToolInvocation(
                id=uuid7(), runner_id=runtime_context.runner_id,
                work_item_id=work_item_id, run_id=run_id,
                session_id=runtime_context.session_id,
                platform_tool_id=definition["platform_tool_id"], tool_key=call.name,
                source_turn_id=source_turn_id, provider_tool_call_id=call.call_id,
                operation_key=operation_key,
                capability_snapshot_id=(
                    capability_snapshot.id if isinstance(capability_snapshot, CapabilitySnapshot)
                    else capability_snapshot.get("id")
                ),
                tool_registration_hash=definition.get("registration_hash"),
                authorization_snapshot={
                    "effective_tool_keys": list(effective),
                    "account_id": runtime_context.account_id,
                    "permission_revision": runtime_context.permission_revision,
                    "authentication_source": runtime_context.authentication_source,
                    "permission_mode": runtime_context.permission_mode,
                },
                execution_mode=definition.get("execution_mode", "inline"), status="created",
                arguments=call.arguments, arguments_hash=arguments_hash,
                business_idempotency_key=operation_key,
                principal_hash=hashlib.sha256(json.dumps({
                    "session_id": runtime_context.session_id,
                    "account_id": runtime_context.account_id,
                    "user_id": runtime_context.user_id,
                    "agent_id": runtime_context.agent_id,
                    "permission_revision": runtime_context.permission_revision,
                }, sort_keys=True).encode()).hexdigest(), attempt_count=0,
            )
            db.add(invocation)
            await db.flush()
            await ExecutionEventWriter(db).append(
                session_id=runtime_context.session_id, run_id=run_id,
                event_type="tool_invocation.created",
                event_dedupe_key=f"tool_invocation:{invocation.id}:created",
                actor_type="agent", actor_id=runtime_context.agent_id,
                entity_type="tool_invocation", entity_id=invocation.id,
                entity_version=invocation.version, visibility="user",
                summary=f"Tool requested: {call.name}",
                payload={"tool_key": call.name, "status": invocation.status},
                correlation_id=run_id,
            )
        confirmation_policy = definition.get("confirmation_policy", "never")
        if confirmation_policy == "always":
            confirmation_policy = "per_call"
        if confirmation_policy == "session_grant" and invocation is not None:
            grants = list(await db.scalars(select(SessionGrant).where(
                SessionGrant.session_id == runtime_context.session_id,
                SessionGrant.target_account_id == runtime_context.account_id,
                SessionGrant.agent_id == runtime_context.agent_id,
                SessionGrant.tool_key == call.name,
                SessionGrant.status == "active",
                SessionGrant.expires_at > utcnow(),
            ).order_by(SessionGrant.expires_at.desc())))
            grant = next((item for item in grants if int(
                (item.scope or {}).get("permission_revision", -1)
            ) == runtime_context.permission_revision), None)
            if grant is not None:
                invocation.authorization_snapshot = {
                    **dict(invocation.authorization_snapshot or {}),
                    "session_grant_id": grant.id,
                }
                confirmation_policy = "never"
        if confirmation_policy in {"per_call", "session_grant"}:
            if invocation is not None:
                invocation.status = "waiting_confirmation"
                subject = {
                    "tool_invocation_id": invocation.id,
                    "tool_key": call.name,
                    "arguments_hash": invocation.arguments_hash,
                    "principal_hash": invocation.principal_hash,
                    "tool_registration_hash": invocation.tool_registration_hash,
                    "permission_revision": runtime_context.permission_revision,
                }
                subject_hash = hashlib.sha256(json.dumps(
                    subject, sort_keys=True, separators=(",", ":")
                ).encode()).hexdigest()
                approval = Approval(
                    id=uuid7(), session_id=runtime_context.session_id,
                    run_id=run_id, work_item_id=work_item_id,
                    requested_by_agent_id=runtime_context.agent_id,
                    kind="session_grant" if confirmation_policy == "session_grant" else "tool",
                    subject_type="tool_invocation", subject_id=invocation.id,
                    subject_version=invocation.version,
                    target_account_id=runtime_context.account_id,
                    subject_hash=subject_hash, request_hash=subject_hash,
                    idempotency_key=operation_key,
                    presentation={
                        "title": f"Approve {call.name}",
                        "summary": "Approve this exact Tool call"
                        if confirmation_policy == "per_call"
                        else "Approve this call and a limited Session grant",
                        "tool_key": call.name, "arguments": call.arguments,
                    },
                    status="pending",
                )
                db.add(approval)
                await db.flush()
                await ExecutionEventWriter(db).append(
                    session_id=runtime_context.session_id, run_id=run_id,
                    event_type="approval.requested",
                    event_dedupe_key=f"approval:{approval.id}:requested",
                    actor_type="agent", actor_id=runtime_context.agent_id,
                    entity_type="approval", entity_id=approval.id,
                    entity_version=approval.version, visibility="user",
                    summary=f"Approval required for {call.name}",
                    payload={
                        "approval_id": approval.id, "type": approval.kind,
                        "work_item_id": work_item_id, "subject_hash": subject_hash,
                    }, correlation_id=run_id,
                )
            return DispatchResult(
                call.call_id, call.name,
                ToolResult(status="succeeded", summary="Tool is waiting for approval", data={
                    "invocation_id": invocation.id if invocation else None,
                    "approval_id": approval.id if invocation else None,
                    "status": "waiting_approval",
                }),
                invocation_id=invocation.id if invocation else None,
                durable_wait=True,
                wait_reason="approval",
            )
        if definition.get("execution_mode") == "durable_async":
            if invocation is not None:
                invocation.status = "queued"
            return DispatchResult(
                call.call_id, call.name,
                ToolResult(status="succeeded", summary="Tool execution queued", data={
                    "invocation_id": invocation.id if invocation else None, "status": "queued",
                }),
                invocation_id=invocation.id if invocation else None,
                durable_wait=True,
                wait_reason="tool",
            )
        if invocation is not None:
            invocation.status = "running"
            invocation.attempt_count = 1
        result = self.executor(call.name, call.arguments, runtime_context, db)
        if inspect.isawaitable(result):
            result = await result
        if invocation is not None:
            invocation.status = result.status
            invocation.result_data = result.model_dump(mode="json")
            if result.error:
                invocation.error_code = result.error.code
                invocation.error_detail = result.error.message
            invocation.version += 1
            await ExecutionEventWriter(db).append(
                session_id=runtime_context.session_id, run_id=run_id,
                event_type=(
                    "tool.succeeded" if result.success else "tool.failed"
                ),
                event_dedupe_key=(
                    f"transition:tool_invocation:{invocation.id}:inline:settle"
                ),
                actor_type="service", actor_id="runtime-action-dispatcher",
                entity_type="tool_invocation", entity_id=invocation.id,
                entity_version=invocation.version, visibility="user",
                summary=(
                    f"Tool completed: {call.name}"
                    if result.success else f"Tool failed: {call.name}"
                ),
                payload={"tool_key": call.name, "status": invocation.status},
                correlation_id=run_id,
            )
        return DispatchResult(
            call.call_id, call.name, result,
            invocation_id=invocation.id if invocation else None,
        )
