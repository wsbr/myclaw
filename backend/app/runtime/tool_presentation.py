"""Project frozen capabilities into the tools currently presented to a model.

Presentation never grants capabilities or rewrites an immutable snapshot.
Execution performs its own locked authorization check after model inference.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.tool_registry.description_policy import project_tool_description
from app.core.tool_registry.base import ToolResult
from app.core.tool_registry.runtime_tools import is_runtime_tool_implementation
from app.models.registry import ToolDefinition, ToolGovernance


def _field(snapshot, name, default=None):
    return snapshot.get(name, default) if isinstance(snapshot, dict) else getattr(snapshot, name, default)


def project_tool_definition(definition: dict[str, Any], *, authentication_source: str) -> dict[str, Any]:
    """Narrow a trusted frozen contract without changing its version identity."""
    projected = deepcopy(definition)
    projected["description"] = project_tool_description(definition)
    if (
        authentication_source == "schedule"
        and definition.get("name") == "schedule_task"
        and definition.get("implementation_ref")
        == "app.core.tool_registry.builtin.schedule_task:ScheduleTaskTool"
    ):
        schema = definition.get("input_schema") or {}
        properties = schema.get("properties") or {}
        projected["input_schema"] = {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["list"]},
                **{key: deepcopy(properties[key]) for key in ("status", "limit") if key in properties},
            },
            "required": ["operation"], "additionalProperties": False,
        }
        projected["description"] = (
            "List existing schedules owned by the requesting user for the current Agent. "
            "This scheduled Run supports only operation=list; schedule changes are unavailable. "
            "Use status and limit to narrow the returned list."
        )
        # This projection admits only the read operation. The frozen version
        # still identifies the full contract, and the Dispatcher repeats it.
        projected["side_effect"] = "none"
    return projected


@dataclass(frozen=True)
class ToolPresentation:
    authorized_tool_versions: tuple[dict[str, Any], ...]
    effective_tool_keys: frozenset[str]
    denied_capabilities: tuple[dict[str, str], ...]
    tool_definitions: tuple[dict[str, Any], ...]


def project_search_result(
    result: ToolResult, presentation: ToolPresentation, *, include_definitions: bool = False,
) -> ToolResult:
    """Refresh discovery observations, including durable/idempotent replays."""
    catalog = {(item["name"], item.get("tool_version_id")): item for item in presentation.authorized_tool_versions}
    denied = {item["capability"]: item["reason_code"] for item in presentation.denied_capabilities}
    matched = [catalog[(item.get("name"), item.get("tool_version_id"))]
               for item in (result.data or {}).get("tools", [])
               if (item.get("name"), item.get("tool_version_id")) in catalog]
    summary = [{
        "name": item["name"], "description": item["description"],
        "tool_version_id": item.get("tool_version_id"),
        "execution_available": item["name"] in presentation.effective_tool_keys,
        "blocked_reason": None if item["name"] in presentation.effective_tool_keys else denied.get(item["name"]),
    } for item in matched]
    return result.model_copy(update={
        "data": {"tools": summary, "loaded_tool_definitions": matched if include_definitions else []},
        "content_blocks": [{"type": "json", "value": summary}],
    })


async def resolve_tool_presentation(
    db, *, capability_snapshot, authentication_source: str,
    tool_definitions: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> ToolPresentation:
    """Intersect frozen admission with live governance and requested versions.

    Passing no requested definitions presents all executable catalog entries. A
    loaded set is matched by both name and frozen version, never by name alone.
"""
    catalog = list(_field(capability_snapshot, "authorized_tool_versions", tool_definitions) or [])
    effective = set(_field(capability_snapshot, "effective_tool_keys", []) or [])
    admitted_search = any(
        item.get("name") == "search_tools" and "search_tools" in effective
        and is_runtime_tool_implementation("search_tools", item.get("implementation_ref"))
        for item in catalog
    )
    denied = {
        item["capability"]: item["reason_code"]
        for item in (_field(capability_snapshot, "denied_capabilities", []) or [])
    }
    ids = {item.get("platform_tool_id") for item in catalog if item.get("platform_tool_id")}
    tools, governance = {}, {}
    if db is not None and ids:
        tools = {row.id: row for row in await db.scalars(select(ToolDefinition).where(
            ToolDefinition.id.in_(ids),
        ).execution_options(populate_existing=True))}
        governance = {row.tool_definition_id: row for row in await db.scalars(select(ToolGovernance).where(
            ToolGovernance.tool_definition_id.in_(ids),
        ).execution_options(populate_existing=True))}
    projected = []
    for original in catalog:
        definition = project_tool_definition(original, authentication_source=authentication_source)
        name, tool_id = definition["name"], definition.get("platform_tool_id")
        if db is not None and tool_id:
            tool, policy = tools.get(tool_id), governance.get(tool_id)
            reason = None
            if tool is None or tool.status != "active" or policy is None or not policy.enabled:
                reason = "platform_tool_disabled"
            elif tool.tool_key != name:
                reason = "tool_binding_invalid"
            else:
                definition.update({
                    "confirmation_policy": policy.confirmation_policy,
                    "confirmation_policy_version": policy.version,
                    "platform_config": {
                        "network_policy": dict(policy.network_policy or {}),
                        "workspace_policy": dict(policy.workspace_policy or {}),
                        "risk_rules": list(policy.risk_rules or []),
                    },
                })
                if is_runtime_tool_implementation(name, definition.get("implementation_ref")) and (
                    policy.confirmation_policy != "never"
                    or any(rule.get("when") == "always" and rule.get("confirmation", "per_call") != "never"
                           for rule in policy.risk_rules or [] if isinstance(rule, dict))
                ):
                    reason = "runtime_confirmation_policy_unsupported"
            if reason:
                effective.discard(name)
                denied[name] = reason
        projected.append(definition)
    requested = None if tool_definitions is None else {
        (item.get("name"), item.get("tool_version_id")) for item in tool_definitions
    }
    # Losing the discovery surface must not make still-authorized non-eager
    # bindings unreachable. This is the same fallback used at snapshot creation.
    if admitted_search and "search_tools" not in effective:
        requested = None
    presented = tuple(item for item in projected if item["name"] in effective and (
        requested is None or (item["name"], item.get("tool_version_id")) in requested
    ))
    return ToolPresentation(
        authorized_tool_versions=tuple(projected), effective_tool_keys=frozenset(effective),
        denied_capabilities=tuple({"capability": name, "reason_code": reason} for name, reason in denied.items()),
        tool_definitions=presented,
    )
