"""Capability, schema, persistence, and execution boundary for native Tool Calls."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tool_registry.base import RuntimeContext, ToolError, ToolResult
from app.core.tool_registry.registry import ToolRegistry
from app.models.common import uuid7
from app.models.runtime_v1 import CapabilitySnapshot, ToolConfirmation, ToolInvocation
from app.runtime.protocol import ToolCallItem


@dataclass(frozen=True)
class DispatchResult:
    call_id: str
    tool_name: str
    result: ToolResult
    invocation_id: str | None = None
    durable_wait: bool = False
    schema_error: bool = False

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
                authorization_snapshot={"effective_tool_keys": list(effective)},
                execution_mode=definition.get("execution_mode", "inline"), status="running",
                arguments=call.arguments, arguments_hash=arguments_hash,
                business_idempotency_key=operation_key,
                principal_hash=hashlib.sha256(
                    json.dumps(runtime_context.model_dump(mode="json"), sort_keys=True).encode()
                ).hexdigest(), attempt_count=1,
            )
            db.add(invocation)
            await db.flush()
        confirmation_policy = definition.get("confirmation_policy", "never")
        if confirmation_policy in {"always", "per_call", "session_grant"}:
            if invocation is not None:
                invocation.status = "waiting_confirmation"
                db.add(ToolConfirmation(
                    id=uuid7(), tool_invocation_id=invocation.id,
                    user_id=runtime_context.user_id, tool_key=call.name,
                    arguments_hash=invocation.arguments_hash,
                    principal_hash=invocation.principal_hash, status="pending",
                    policy_version=int(definition.get("confirmation_policy_version", 1)),
                ))
                await db.flush()
            return DispatchResult(
                call.call_id, call.name,
                ToolResult(status="succeeded", summary="Tool is waiting for approval", data={
                    "invocation_id": invocation.id if invocation else None,
                    "status": "waiting_confirmation",
                }),
                invocation_id=invocation.id if invocation else None,
                durable_wait=True,
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
            )
        result = self.executor(call.name, call.arguments, runtime_context, db)
        if inspect.isawaitable(result):
            result = await result
        if invocation is not None:
            invocation.status = result.status
            invocation.result_data = result.model_dump(mode="json") if result.success else None
            if result.error:
                invocation.error_code = result.error.code
                invocation.error_detail = result.error.message
        return DispatchResult(
            call.call_id, call.name, result,
            invocation_id=invocation.id if invocation else None,
        )
