"""Capability, schema, persistence, and execution boundary for native Tool Calls."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tool_registry.base import RuntimeContext, ToolError, ToolResult
from app.core.tool_registry.registry import ToolRegistry
from app.core.tool_registry.schema import validation_error_refs, validation_errors
from app.core.platform_secrets import secret_reference, store_secret
from app.models.common import utcnow, uuid7
from app.models.foundation import UserSession
from app.models.runtime_v1 import (
    Approval, BrowserSessionState, CapabilitySnapshot, Run, SessionGrant, ToolInvocation,
)
from app.persistence.reliable_writes import ExecutionEventWriter
from app.runtime.coordination_tools import dispatch_coordination_tool
from app.runtime.protocol import ToolCallItem
from app.services.run_reliability_service import BudgetExceeded, assert_action_budget


@dataclass(frozen=True)
class DispatchResult:
    call_id: str
    tool_name: str
    result: ToolResult
    invocation_id: str | None = None
    durable_wait: bool = False
    schema_error: bool = False
    wait_reason: str | None = None

    def provider_message(self, *, project: bool = True) -> dict[str, Any]:
        result = self.result
        if project and self.tool_name in {"web_search", "web_fetch"}:
            from app.runtime.web_result_projection import (
                BALANCED_SINGLE_FETCH_TOKENS,
                BALANCED_SINGLE_SEARCH_TOKENS,
                project_web_result,
            )

            result = project_web_result(
                self.tool_name, result,
                max_tokens=(
                    BALANCED_SINGLE_FETCH_TOKENS
                    if self.tool_name == "web_fetch"
                    else BALANCED_SINGLE_SEARCH_TOKENS
                ),
            )
        return {
            "role": "tool",
            "tool_use_id": self.call_id,
            "tool_call_id": self.call_id,
            "content": json.dumps(result.model_dump(mode="json"), ensure_ascii=False),
        }


Executor = Callable[[str, dict[str, Any], RuntimeContext, AsyncSession | None], Awaitable[ToolResult]]
ProgressObserver = Callable[[dict[str, Any]], Awaitable[None]]

WEB_RETRIEVAL_LIMITS = {
    "web_search": {"turn": 2, "normal_run": 2, "run": 6},
    "web_fetch": {"turn": 3, "normal_run": 3, "run": 8},
}

SCHEDULE_MUTATING_OPERATIONS = frozenset({
    "create", "update", "pause", "resume", "delete", "run_now",
})

# Runtime-owned compatibility policy for frozen built-in ToolVersions. New
# versions also advertise this through annotations, but already-published
# AgentVersions must receive correctness fixes without being republished.
LOSSLESS_NORMALIZATION_TOOL_KEYS = frozenset({"schedule_task"})


def _scheduled_schedule_mutation_failure(
    call: ToolCallItem, authentication_source: str,
) -> DispatchResult | None:
    """Reject schedule recursion before creating an Approval or Invocation."""

    if (
        authentication_source != "schedule"
        or call.name != "schedule_task"
        or call.arguments.get("operation") not in SCHEDULE_MUTATING_OPERATIONS
    ):
        return None
    return DispatchResult(
        call.call_id, call.name,
        ToolResult(
            status="failed",
            summary="Scheduled runs cannot mutate schedules",
            error=ToolError(
                code="schedule_recursive_mutation_denied",
                message=(
                    "A platform-scheduled run cannot create, update, pause, resume, "
                    "delete, or immediately run another schedule"
                ),
                category="authorization",
                details={"authentication_source": authentication_source},
            ),
        ),
    )


def _safe_argument_projection(value: Any, *, force_text_redaction: bool = False) -> Any:
    """Return a UI/log-safe projection while hashes continue binding raw input."""

    sensitive = ("password", "secret", "token", "api_key", "credential", "authorization")
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).casefold()
            if any(marker in lowered for marker in sensitive) or (
                force_text_redaction and key == "text"
            ):
                projected[key] = "[REDACTED]"
            else:
                projected[key] = _safe_argument_projection(item)
        return projected
    if isinstance(value, list):
        return [_safe_argument_projection(item) for item in value]
    return value


def _schema_errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    del path
    return [
        f"{item['path'] or '/'}: {item['message']}"
        for item in validation_errors(value, schema)
    ]


_CANONICAL_INTEGER_STRING = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")


def _json_pointer(path: str, key: object) -> str:
    escaped = str(key).replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}" if path else f"/{escaped}"


def _lossless_argument_normalization(
    value: Any, schema: dict[str, Any], *, path: str = "",
) -> tuple[Any, list[dict[str, str]]]:
    """Apply only unambiguous JSON scalar repairs enabled by Tool metadata.

    The provider-authored value remains immutable in ``AgentTurn.turn_items``.
    Durable invocations persist the repair manifest alongside their authorization
    snapshot so execution never disguises what the model originally emitted.
    """

    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema, dict) else None
        properties = properties if isinstance(properties, dict) else {}
        normalized: dict[str, Any] = {}
        repairs: list[dict[str, str]] = []
        for key, item in value.items():
            child_schema = properties.get(key)
            child_schema = child_schema if isinstance(child_schema, dict) else {}
            child, child_repairs = _lossless_argument_normalization(
                item, child_schema, path=_json_pointer(path, key),
            )
            normalized[key] = child
            repairs.extend(child_repairs)
        return normalized, repairs
    if isinstance(value, list):
        item_schema = schema.get("items") if isinstance(schema, dict) else None
        item_schema = item_schema if isinstance(item_schema, dict) else {}
        normalized_items = []
        repairs = []
        for index, item in enumerate(value):
            child, child_repairs = _lossless_argument_normalization(
                item, item_schema, path=_json_pointer(path, index),
            )
            normalized_items.append(child)
            repairs.extend(child_repairs)
        return normalized_items, repairs

    declared = schema.get("type") if isinstance(schema, dict) else None
    declared_types = {declared} if isinstance(declared, str) else set(declared or [])
    if (
        isinstance(value, str)
        and "integer" in declared_types
        and "string" not in declared_types
        and _CANONICAL_INTEGER_STRING.fullmatch(value) is not None
    ):
        return int(value), [{
            "path": path or "/",
            "from_type": "string",
            "to_type": "integer",
            "rule": "canonical_integer_string",
        }]
    return value, []


def _remove_optional_nulls(value: Any, schema: dict[str, Any]) -> Any:
    """Undo OpenAI strict-mode nullable placeholders before neutral validation."""

    if not isinstance(value, dict) or not isinstance(schema, dict):
        return value
    required = set(schema.get("required") or [])
    properties = schema.get("properties") or {}
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        child = properties.get(key) or {}
        if item is None and key not in required:
            continue
        if isinstance(item, dict):
            normalized[key] = _remove_optional_nulls(item, child)
        elif isinstance(item, list) and isinstance(child.get("items"), dict):
            normalized[key] = [
                _remove_optional_nulls(element, child["items"])
                for element in item
            ]
        else:
            normalized[key] = item
    return normalized


def _prepare_tool_call(
    call: ToolCallItem, definition: dict[str, Any],
) -> tuple[ToolCallItem, list[dict[str, str]]]:
    schema = definition.get("input_schema") or {}
    arguments = _remove_optional_nulls(call.arguments, schema)
    repairs: list[dict[str, str]] = []
    if (
        call.name in LOSSLESS_NORMALIZATION_TOOL_KEYS
        or (definition.get("annotations") or {}).get(
            "lossless_argument_normalization"
        ) is True
    ):
        arguments, repairs = _lossless_argument_normalization(arguments, schema)
    if arguments != call.arguments:
        call = replace(call, arguments=arguments)
    return call, repairs


def _schema_failure(call: ToolCallItem, schema: dict[str, Any]) -> DispatchResult:
    issues = validation_errors(call.arguments, schema)
    errors = [
        f"{item['path'] or '/'}: {item['message']}"
        for item in issues
    ]
    repair_instruction = (
        "Correct the arguments and call the same Tool again. Do not repeat the "
        "unchanged arguments. Use native JSON types exactly as declared by the "
        "Tool schema (for example, use 10 rather than \"10\" for an integer)."
    )
    return DispatchResult(
        call.call_id, call.name,
        ToolResult(
            status="failed", summary="Tool arguments failed schema validation",
            error=ToolError(
                code="tool_arguments_schema_error",
                message=f"{repair_instruction} Validation errors: {'; '.join(errors)}",
                category="validation",
                details={
                    "errors": errors,
                    "issues": [{
                        "path": item["path"],
                        "schema_path": item["schema_path"],
                        "validator": item["validator"],
                    } for item in issues],
                    "repair_instruction": repair_instruction,
                },
            ),
        ), schema_error=True,
    )


async def _dynamic_confirmation(
    db: AsyncSession | None, *, definition: dict[str, Any], arguments: dict[str, Any],
    context: RuntimeContext,
) -> tuple[str, dict[str, Any]]:
    policy = definition.get("confirmation_policy", "never")
    risk: dict[str, Any] = {"reasons": []}
    if definition.get("name") == "browser_interact" and db is not None:
        state = await db.get(BrowserSessionState, context.session_id)
        if state is not None:
            elements = {
                item.get("ref"): item
                for item in (state.last_operation or {}).get("elements", [])
                if isinstance(item, dict)
            }
            element = elements.get(arguments.get("element_ref")) or {}
            action = arguments.get("action")
            name = str(element.get("name") or "").casefold()
            sensitive_markers = (
                "submit", "sign in", "log in", "pay", "purchase", "buy", "send",
                "publish", "post", "delete", "remove", "cancel", "withdraw", "confirm",
                "提交", "登录", "支付", "购买", "发送", "发布", "删除", "撤销", "确认",
            )
            reasons = []
            if action in {"upload", "download"}:
                reasons.append(action)
            if action == "input" and str(element.get("type") or "").casefold() == "password":
                reasons.append("login_credential")
            if action in {"click", "press"} and (
                str(element.get("form_method") or "").upper() not in {"", "GET"}
                or str(element.get("type") or "").casefold() in {"submit", "image"}
                or any(marker in name for marker in sensitive_markers)
                or (action == "press" and str(arguments.get("key") or "").casefold() == "enter")
            ):
                reasons.append("external_side_effect")
            if reasons:
                policy = "per_call"
            risk = {
                "reasons": reasons, "origin": state.current_origin,
                "page_revision": state.page_revision,
                "dom_fingerprint": state.dom_fingerprint,
                "element": element,
            }
    configured_rules = (
        (definition.get("platform_config") or {}).get("risk_rules") or []
    )
    for index, rule in enumerate(configured_rules):
        if not isinstance(rule, dict):
            continue
        condition = rule.get("when")
        matches = condition == "always"
        if condition == "unknown_side_effect":
            matches = bool(
                (definition.get("annotations") or {}).get("mcp_annotations_untrusted")
                or definition.get("side_effect") == "non_idempotent"
            )
        elif isinstance(condition, dict):
            key = condition.get("argument")
            present = isinstance(key, str) and key in arguments
            matches = bool(present)
            if "equals" in condition:
                matches = present and arguments.get(key) == condition["equals"]
            elif "in" in condition and isinstance(condition["in"], list):
                matches = present and arguments.get(key) in condition["in"]
            elif "exists" in condition:
                matches = present is bool(condition["exists"])
        requested = rule.get("confirmation", "per_call")
        if matches and requested in {"per_call", "session_grant"}:
            if requested == "per_call" or policy == "never":
                policy = requested
            risk.setdefault("reasons", []).append(f"governance_rule:{index}")
    return policy, risk


async def _builtin_executor(name, arguments, context, db):
    return await ToolRegistry.execute(name, arguments, context, db=db)


def _approval_presentation(
    *, tool_name: str, arguments: dict[str, Any], confirmation_policy: str,
    dynamic_risk: dict[str, Any], force_text_redaction: bool,
) -> dict[str, Any]:
    if tool_name == "schedule_task":
        operation = str(arguments.get("operation") or "")
        labels = {
            "create": "创建", "update": "修改", "pause": "暂停",
            "resume": "恢复", "delete": "删除", "run_now": "立即运行",
        }
        action = labels.get(operation, "管理")
        title = f"确认{action}定时任务"
        summary = (
            f"智能体将代表你{action}一项持久化定时任务；请核对时间、内容和上下文方式。"
        )
    else:
        title = f"Approve {tool_name}"
        summary = (
            "Approve this exact Tool call"
            if confirmation_policy == "per_call"
            else "Approve this call and a limited Session grant"
        )
    return {
        "title": title,
        "summary": summary,
        "tool_key": tool_name,
        "arguments": _safe_argument_projection(
            arguments, force_text_redaction=force_text_redaction,
        ),
        "risk": dynamic_risk,
    }


async def _record_authorization_rejection(
    db: AsyncSession | None, *, call: ToolCallItem, context: RuntimeContext,
    run_id: str | None, source_turn_id: str | None, error_code: str,
    error_category: str,
) -> None:
    """Project a rejected Tool call without manufacturing an invocation fact."""

    if db is None or not run_id or not source_turn_id:
        return
    await db.scalar(select(UserSession).where(
        UserSession.id == context.session_id,
    ).execution_options(populate_existing=True).with_for_update())
    arguments_hash = hashlib.sha256(json.dumps(
        call.arguments, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    await ExecutionEventWriter(db).append(
        session_id=context.session_id, run_id=run_id,
        event_type="tool_invocation.rejected",
        event_dedupe_key=(
            f"tool-rejection:{source_turn_id}:{call.call_id}:{error_code}"
        ),
        actor_type="service", actor_id="runtime-action-dispatcher",
        entity_type="agent_turn", entity_id=source_turn_id,
        visibility="user", summary=f"Tool request rejected: {call.name}",
        payload={
            "tool_key": call.name, "error_code": error_code,
            "error_category": error_category,
            "arguments_hash": arguments_hash,
        },
        correlation_id=run_id,
    )


class ActionDispatcher:
    def __init__(
        self, tool_definitions: list[dict[str, Any]], *,
        executor: Executor = _builtin_executor,
        progress_observer: ProgressObserver | None = None,
    ) -> None:
        self.definitions = {value["name"]: value for value in tool_definitions}
        self.executor = executor
        self.progress_observer = progress_observer

    async def _progress(
        self, call: ToolCallItem, status: str,
        invocation_id: str | None = None,
    ) -> None:
        if self.progress_observer is not None:
            await self.progress_observer({
                "call_id": call.call_id, "tool_key": call.name,
                "status": status, "tool_invocation_id": invocation_id,
            })

    def add_definitions(self, definitions: list[dict[str, Any]]) -> None:
        """Load only definitions already admitted by the CapabilitySnapshot."""
        for definition in definitions:
            self.definitions[definition["name"]] = dict(definition)

    def _preflight(
        self, call: ToolCallItem, *, effective: set[str], permission_mode: str,
        authentication_source: str,
    ) -> DispatchResult | None:
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
            return _schema_failure(
                call, definition.get("input_schema") or {},
            )
        if permission_mode == "plan_readonly" and definition.get("side_effect", "none") != "none":
            return DispatchResult(
                call.call_id, call.name,
                ToolResult(
                    status="failed", summary="Plan read-only mode denied this side effect",
                    error=ToolError(
                        code="permission_mode_denied",
                        message=f"Tool '{call.name}' has side effects before Plan approval",
                        category="authorization", details={"permission_mode": permission_mode},
                    ),
                ),
            )
        recursive_schedule = _scheduled_schedule_mutation_failure(
            call, authentication_source,
        )
        if recursive_schedule is not None:
            return recursive_schedule
        return None

    async def _apply_web_retrieval_limits(
        self, calls: list[ToolCallItem], *, db: AsyncSession | None,
        runtime_context: RuntimeContext, run_id: str | None,
        source_turn_id: str | None,
    ) -> tuple[list[ToolCallItem], dict[str, DispatchResult]]:
        if db is None or not run_id or not source_turn_id or not any(
            call.name in WEB_RETRIEVAL_LIMITS for call in calls
        ):
            return calls, {}
        # Serialize admission on the existing Session -> Run aggregate lock
        # order so concurrent Group members cannot race the per-Run ceiling.
        await db.scalar(select(UserSession).where(
            UserSession.id == runtime_context.session_id,
        ).execution_options(populate_existing=True).with_for_update())
        await db.scalar(select(Run).where(
            Run.id == run_id,
        ).execution_options(populate_existing=True).with_for_update())
        existing = list(await db.scalars(select(ToolInvocation).where(
            ToolInvocation.run_id == run_id,
            ToolInvocation.tool_key.in_(tuple(WEB_RETRIEVAL_LIMITS)),
        )))
        run_counts = {
            name: sum(item.tool_key == name for item in existing)
            for name in WEB_RETRIEVAL_LIMITS
        }
        turn_counts = {
            name: sum(
                item.tool_key == name and item.source_turn_id == source_turn_id
                for item in existing
            )
            for name in WEB_RETRIEVAL_LIMITS
        }
        failed_counts = {
            name: sum(
                item.tool_key == name
                and item.status in {"failed", "cancelled", "outcome_unknown"}
                for item in existing
            )
            for name in WEB_RETRIEVAL_LIMITS
        }
        existing_calls = {
            (item.tool_key, item.source_turn_id, item.provider_tool_call_id)
            for item in existing
        }
        admitted: list[ToolCallItem] = []
        rejected: dict[str, DispatchResult] = {}
        for call in calls:
            limits = WEB_RETRIEVAL_LIMITS.get(call.name)
            if limits is None or (
                call.name, source_turn_id, call.call_id
            ) in existing_calls:
                admitted.append(call)
                continue
            dimension = None
            limit = None
            used = None
            if turn_counts[call.name] >= limits["turn"]:
                dimension, limit, used = "agent_turn", limits["turn"], turn_counts[call.name]
            elif run_counts[call.name] >= limits["run"]:
                dimension, limit, used = "run", limits["run"], run_counts[call.name]
            elif run_counts[call.name] >= min(
                limits["run"], limits["normal_run"] + failed_counts[call.name],
            ):
                # The wider 6/8 Run ceilings are replacement capacity, not an
                # exploration allowance. Every call beyond the normal 2/3
                # envelope must replace a prior typed terminal failure.
                dimension = "run_recovery_reserve"
                limit = min(
                    limits["run"], limits["normal_run"] + failed_counts[call.name],
                )
                used = run_counts[call.name]
            if dimension is not None:
                result = DispatchResult(
                    call.call_id, call.name,
                    ToolResult(
                        status="failed", summary="Balanced web retrieval limit exceeded",
                        error=ToolError(
                            code="web_retrieval_limit_exceeded",
                            message=(
                                f"{call.name} exceeded its per-{dimension} Balanced limit"
                            ),
                            category="authorization",
                            details={"dimension": dimension, "limit": limit, "used": used},
                        ),
                    ),
                )
                rejected[call.call_id] = result
                await _record_authorization_rejection(
                    db, call=call, context=runtime_context, run_id=run_id,
                    source_turn_id=source_turn_id,
                    error_code="web_retrieval_limit_exceeded",
                    error_category="authorization",
                )
                continue
            admitted.append(call)
            turn_counts[call.name] += 1
            run_counts[call.name] += 1
        return admitted, rejected

    async def dispatch_batch(
        self, calls: list[ToolCallItem], *,
        capability_snapshot: CapabilitySnapshot | dict[str, Any],
        runtime_context: RuntimeContext, db: AsyncSession | None = None,
        run_id: str | None = None, work_item_id: str | None = None,
        source_turn_id: str | None = None,
    ) -> list[DispatchResult]:
        """Preflight the whole batch before admitting any side effect.

        Read-only durable calls are enqueued first so independent Sandbox
        workers may execute them concurrently. Browser, Workspace mutations,
        and other side-effecting calls preserve provider order.
        """
        effective = set(
            capability_snapshot.effective_tool_keys
            if isinstance(capability_snapshot, CapabilitySnapshot)
            else capability_snapshot.get("effective_tool_keys", [])
        )
        rejected: dict[str, DispatchResult] = {}
        admitted: list[ToolCallItem] = []
        argument_normalizations: dict[str, list[dict[str, str]]] = {}
        for original_call in calls:
            definition = self.definitions.get(original_call.name) or {}
            call, repairs = _prepare_tool_call(original_call, definition)
            argument_normalizations[call.call_id] = repairs
            failure = self._preflight(
                call, effective=effective, permission_mode=runtime_context.permission_mode,
                authentication_source=runtime_context.authentication_source,
            )
            if failure is not None:
                rejected[call.call_id] = failure
                error = failure.result.error
                if error and error.category in {"authorization", "validation"}:
                    await _record_authorization_rejection(
                        db, call=call, context=runtime_context, run_id=run_id,
                        source_turn_id=source_turn_id,
                        error_code=error.code, error_category=error.category,
                    )
            else:
                admitted.append(call)
        admitted, web_rejected = await self._apply_web_retrieval_limits(
            admitted, db=db, runtime_context=runtime_context,
            run_id=run_id, source_turn_id=source_turn_id,
        )
        rejected.update(web_rejected)
        # Risk classification is part of whole-batch preflight. Execution
        # repeats it immediately before admission so a Browser revision or
        # governance change cannot rely on this earlier snapshot.
        for call in admitted:
            await _dynamic_confirmation(
                db, definition=self.definitions[call.name],
                arguments=call.arguments, context=runtime_context,
            )
        # No admitted Tool may run until every call has completed Schema,
        # capability, permission, and resource-class preflight.
        readonly: list[ToolCallItem] = []
        serialized: list[ToolCallItem] = []
        for call in admitted:
            definition = self.definitions[call.name]
            workspace_access = ((definition.get("platform_config") or {}).get("workspace_policy") or {}).get("workspace_access")
            if (
                definition.get("side_effect", "none") == "none"
                and not call.name.startswith("browser_")
                and workspace_access != "write"
            ):
                readonly.append(call)
            else:
                serialized.append(call)
        completed: dict[str, DispatchResult] = dict(rejected)
        for call in calls:
            if call.call_id in rejected:
                await self._progress(call, "failed")
        for call in [*readonly, *serialized]:
            await self._progress(call, "running")
            dispatched = await self.dispatch(
                call, capability_snapshot=capability_snapshot,
                runtime_context=runtime_context, db=db, run_id=run_id,
                work_item_id=work_item_id, source_turn_id=source_turn_id,
                argument_normalizations=argument_normalizations.get(call.call_id),
            )
            completed[call.call_id] = dispatched
            status = str((dispatched.result.data or {}).get("status") or dispatched.result.status)
            if status not in {"queued", "running", "succeeded", "failed"}:
                status = "queued" if dispatched.durable_wait else (
                    "succeeded" if dispatched.result.success else "failed"
                )
            await self._progress(call, status, dispatched.invocation_id)
        return [completed[call.call_id] for call in calls]

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
        argument_normalizations: list[dict[str, str]] | None = None,
    ) -> DispatchResult:
        effective = (
            capability_snapshot.effective_tool_keys
            if isinstance(capability_snapshot, CapabilitySnapshot)
            else capability_snapshot.get("effective_tool_keys", [])
        )
        definition = self.definitions.get(call.name)
        if call.name not in effective or definition is None:
            await _record_authorization_rejection(
                db, call=call, context=runtime_context, run_id=run_id,
                source_turn_id=source_turn_id,
                error_code="tool_not_in_capability_snapshot",
                error_category="authorization",
            )
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
        if argument_normalizations is None:
            call, argument_normalizations = _prepare_tool_call(call, definition)
        errors = _schema_errors(call.arguments, definition.get("input_schema") or {})
        if errors:
            return _schema_failure(call, definition.get("input_schema") or {})

        # CapabilitySnapshot is the first authorization layer.  Re-evaluate
        # the actual semantic side-effect class at the Dispatcher boundary so
        # a stale, forged, or incorrectly assembled snapshot still cannot turn
        # plan_readonly into a successful mutation.
        if (
            runtime_context.permission_mode == "plan_readonly"
            and definition.get("side_effect", "none") != "none"
        ):
            await _record_authorization_rejection(
                db, call=call, context=runtime_context, run_id=run_id,
                source_turn_id=source_turn_id,
                error_code="permission_mode_denied",
                error_category="authorization",
            )
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

        recursive_schedule = _scheduled_schedule_mutation_failure(
            call, runtime_context.authentication_source,
        )
        if recursive_schedule is not None:
            await _record_authorization_rejection(
                db, call=call, context=runtime_context, run_id=run_id,
                source_turn_id=source_turn_id,
                error_code="schedule_recursive_mutation_denied",
                error_category="authorization",
            )
            return recursive_schedule

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
                        error_category="provider",
                    ),
                )
            if call.name == "invoke_agent":
                try:
                    await assert_action_budget(db, run_id, action_kind="agent")
                except BudgetExceeded as error:
                    return DispatchResult(
                        call.call_id, call.name,
                        ToolResult(
                            status="failed", summary=str(error),
                            error=ToolError(
                                code=error.code, message=str(error),
                                category="authorization",
                                details={
                                    "dimension": error.dimension,
                                    "limit": error.limit, "used": error.used,
                                },
                            ),
                        ),
                    )
            result, durable_wait, wait_reason = await dispatch_coordination_tool(
                call.name, call.arguments, context=runtime_context, db=db,
                run_id=run_id, work_item_id=work_item_id,
                source_turn_id=source_turn_id, provider_call_id=call.call_id,
            )
            output_issues = validation_error_refs(
                result.model_dump(mode="json"), definition["output_schema"],
            )
            if output_issues:
                result = ToolResult(
                    status="failed", summary="Runtime Tool returned an invalid result",
                    error="Runtime Tool output failed its registered schema",
                    error_code="tool_output_schema_error", error_category="provider",
                    error_details={"errors": output_issues},
                )
                durable_wait = False
                wait_reason = None
            return DispatchResult(
                call.call_id, call.name, result,
                durable_wait=durable_wait, wait_reason=wait_reason,
            )

        confirmation_policy, dynamic_risk = await _dynamic_confirmation(
            db, definition=definition, arguments=call.arguments, context=runtime_context,
        )
        invocation = None
        if db is not None and run_id and work_item_id and source_turn_id:
            encoded = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            arguments_hash = hashlib.sha256(encoded.encode()).hexdigest()
            operation_key = f"turn:{source_turn_id}:call:{call.call_id}"
            existing = await db.scalar(select(ToolInvocation).where(
                ToolInvocation.work_item_id == work_item_id,
                ToolInvocation.business_idempotency_key == operation_key,
            ).with_for_update())
            if existing is not None:
                if (
                    existing.arguments_hash != arguments_hash
                    or existing.tool_key != call.name
                ):
                    return DispatchResult(
                        call.call_id, call.name,
                        ToolResult(
                            status="failed", summary="Operation key conflicts with another request",
                            error=ToolError(
                                code="idempotency_conflict",
                                message="The existing operation key has different Tool arguments",
                                category="conflict", details={
                                    "invocation_id": existing.id,
                                },
                            ),
                        ),
                        invocation_id=existing.id,
                    )
                terminal = {
                    "succeeded", "failed", "rejected", "cancelled", "outcome_unknown",
                }
                if existing.status in terminal and existing.result_data:
                    replayed = ToolResult.model_validate(existing.result_data)
                    return DispatchResult(
                        call.call_id, call.name, replayed,
                        invocation_id=existing.id,
                    )
                return DispatchResult(
                    call.call_id, call.name,
                    ToolResult(
                        status="succeeded", summary="Existing Tool operation is still durable",
                        data={"invocation_id": existing.id, "status": existing.status},
                    ),
                    invocation_id=existing.id, durable_wait=True,
                    wait_reason=(
                        "approval" if existing.status == "waiting_confirmation" else "tool"
                    ),
                )
            owning_run = await db.get(Run, run_id)
            if owning_run is not None:
                try:
                    await assert_action_budget(db, run_id, action_kind="tool")
                except BudgetExceeded as error:
                    return DispatchResult(
                        call.call_id, call.name,
                        ToolResult(
                            status="failed", summary=str(error),
                            error=ToolError(
                                code=error.code, message=str(error), category="authorization",
                                details={
                                    "dimension": error.dimension,
                                    "limit": error.limit, "used": error.used,
                                },
                            ),
                        ),
                    )
            stored_arguments = call.arguments
            if (
                "login_credential" in dynamic_risk.get("reasons", [])
                and call.name == "browser_interact"
                and isinstance(call.arguments.get("text"), str)
            ):
                browser_secret = await store_secret(
                    db, plaintext=call.arguments["text"],
                    purpose="browser_input",
                    created_by_account_id=runtime_context.account_id,
                )
                stored_arguments = {
                    **call.arguments,
                    "text": secret_reference(browser_secret),
                }
            invocation = ToolInvocation(
                id=uuid7(), tenant_id=runtime_context.tenant_id,
                account_id=runtime_context.account_id,
                runner_id=runtime_context.runner_id,
                work_item_id=work_item_id, run_id=run_id,
                session_id=runtime_context.session_id,
                platform_tool_id=definition["platform_tool_id"], tool_key=call.name,
                tool_version_id=definition.get("tool_version_id"),
                tool_version_content_hash=definition.get("tool_version_content_hash"),
                connection_id=definition.get("connection_id"),
                connection_revision_id=definition.get("connection_revision_id"),
                secret_fingerprint=definition.get("secret_fingerprint"),
                source_turn_id=source_turn_id, provider_tool_call_id=call.call_id,
                operation_key=operation_key,
                capability_snapshot_id=(
                    capability_snapshot.id if isinstance(capability_snapshot, CapabilitySnapshot)
                    else capability_snapshot.get("id")
                ),
                authorization_snapshot={
                    "effective_tool_keys": list(effective),
                    "tenant_id": runtime_context.tenant_id,
                    "account_id": runtime_context.account_id,
                    "permission_revision": runtime_context.permission_revision,
                    "authentication_source": runtime_context.authentication_source,
                    "permission_mode": runtime_context.permission_mode,
                    "argument_normalizations": list(argument_normalizations or []),
                },
                execution_mode=definition.get("execution_mode", "inline"), status="created",
                arguments=stored_arguments, arguments_hash=arguments_hash,
                business_idempotency_key=operation_key,
                principal_hash=hashlib.sha256(json.dumps({
                    "tenant_id": runtime_context.tenant_id,
                    "session_id": runtime_context.session_id,
                    "account_id": runtime_context.account_id,
                    "user_id": runtime_context.user_id,
                    "agent_id": runtime_context.agent_id,
                    "permission_revision": runtime_context.permission_revision,
                }, sort_keys=True).encode()).hexdigest(), attempt_count=0,
                execution_epoch=runtime_context.execution_epoch,
                deadline_at=owning_run.deadline_at if owning_run else None,
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
                payload={
                    "tool_key": call.name, "status": invocation.status,
                    "argument_normalization_count": len(
                        argument_normalizations or []
                    ),
                },
                correlation_id=run_id,
            )
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
            if invocation is None:
                return DispatchResult(
                    call.call_id, call.name,
                    ToolResult(
                        status="failed",
                        summary="Durable approval context is required",
                        error="A confirmation-required Tool cannot execute without a durable database context",
                        error_code="confirmation_context_required",
                        error_category="confirmation",
                    ),
                )
            if invocation is not None:
                invocation.status = "waiting_confirmation"
                subject = {
                    "tool_invocation_id": invocation.id,
                    "tool_key": call.name,
                    "arguments_hash": invocation.arguments_hash,
                    "principal_hash": invocation.principal_hash,
                    "tool_version_id": invocation.tool_version_id,
                    "tool_version_content_hash": invocation.tool_version_content_hash,
                    "connection_revision_id": invocation.connection_revision_id,
                    "secret_fingerprint": invocation.secret_fingerprint,
                    "permission_revision": runtime_context.permission_revision,
                    "dynamic_risk": dynamic_risk,
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
                    presentation=_approval_presentation(
                        tool_name=call.name, arguments=call.arguments,
                        confirmation_policy=confirmation_policy,
                        dynamic_risk=dynamic_risk,
                        force_text_redaction=(
                            "login_credential" in dynamic_risk.get("reasons", [])
                        ),
                    ),
                    status="pending",
                    deadline_at=owning_run.deadline_at if owning_run else None,
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
        execution_context = runtime_context.model_copy(update={
            "tool_invocation_id": invocation.id if invocation else None,
            "tool_version_id": definition.get("tool_version_id"),
            "tool_connection_revision_id": definition.get("connection_revision_id"),
        })
        result = self.executor(call.name, call.arguments, execution_context, db)
        if inspect.isawaitable(result):
            result = await result
        output_issues = validation_error_refs(
            result.model_dump(mode="json"), definition["output_schema"],
        )
        if output_issues:
            result = ToolResult(
                status="failed", summary="Tool returned an invalid result",
                error="Tool output failed its immutable registered schema",
                error_code="tool_output_schema_error", error_category="provider",
                error_details={"errors": output_issues},
            )
        if (
            call.name == "memory_propose"
            and result.success
            and db is not None
            and isinstance(result.data, dict)
            and result.data.get("candidate_id")
        ):
            # memory_propose only creates the user-sourced candidate. The
            # platform adjudicator, not the model-facing Tool, applies the
            # conflict/version/user-edit policy in the same durable commit.
            from app.services.memory_service import apply_memory_candidate

            await apply_memory_candidate(db, result.data["candidate_id"])
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
