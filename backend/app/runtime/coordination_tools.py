"""Runtime-owned coordination Tool definitions and persistence handlers."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.core.tool_registry.base import RuntimeContext, ToolResult
from app.core.tool_registry.schema import runtime_result_schema
from app.models.common import uuid7
from app.models.runtime_v1 import CapabilitySnapshot, DurableRunner, Clarification, Message, Run, WorkItem
from app.persistence.reliable_writes import ExecutionEventWriter
from app.services.working_plan_service import WorkingPlanConflict, update_working_plan
from app.services.agent_directory_service import agent_card_view, discover_agent_cards
from app.services.agent_invocation_service import (
    AgentInvocationRejected, dispatch_agent_invocation,
    persist_rejected_agent_invocation,
)
from app.services.group_collaboration_service import (
    GroupPolicyError,
    reject_fixed_run_invitation,
    send_agent_group_message,
    update_runtime_shared_task,
)


RUNTIME_COORDINATION_DEFINITIONS = ({
    "name": "search_tools",
    "description": (
        "WHEN: a needed capability is not currently visible, or inventory the authorized catalog with an empty "
        "query. WHEN NOT: do not search for an already presented Tool. "
        "RETURNS: matching pre-authorized frozen ToolVersions plus current execution availability; executable "
        "schemas are presented next Turn, while temporarily blocked Tools remain discoverable for planning. "
        "RECOVERY: refine the capability query; discovery never expands authorization."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 500, "default": ""},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "additionalProperties": False,
    },
    "source": "runtime",
}, {
    "name": "update_plan",
    "description": (
        "WHEN: create or revise the visible working plan for a multi-step task. "
        "WHEN NOT: skip it for a simple one-step request. RETURNS: the persisted WorkingPlan revision. "
        "RECOVERY: refresh the current revision after a conflict."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": 2000},
            "items": {
                "type": "array", "minItems": 1, "maxItems": 100,
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["text", "status"],
                    "additionalProperties": False,
                },
            },
            "explanation": {"type": "string", "maxLength": 4000},
            "expected_revision": {"type": "integer", "minimum": 0},
        },
        "required": ["items"],
        "additionalProperties": False,
    },
    "source": "runtime",
}, {
    "name": "ask_user",
    "description": (
        "WHEN: one essential user decision is missing. WHEN NOT: do not ask for discoverable facts. "
        "RETURNS: a durable clarification wait. RECOVERY: continue only after the user's committed answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 8000},
        },
        "required": ["question"],
        "additionalProperties": False,
    },
    "source": "runtime",
}, {
    "name": "discover_agents",
    "description": (
        "WHEN: find a governed professional Agent for a bounded subtask. WHEN NOT: do not use for Platform Tools. "
        "RETURNS: currently visible Agent cards. RECOVERY: refine the query; discovery does not authorize invocation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 500},
            "domain_tags": {
                "type": "array", "maxItems": 20,
                "items": {"type": "string", "minLength": 1, "maxLength": 100},
            },
            "group_enabled": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "additionalProperties": False,
    },
    "source": "runtime",
}, {
    "name": "invoke_agent",
    "description": (
        "WHEN: delegate a bounded request to an authorized discovered Agent. WHEN NOT: do not invoke unknown IDs. "
        "RETURNS: a durable AgentInvocation wait. RECOVERY: inspect authorization or the returned Agent result."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "minLength": 1, "maxLength": 191},
            "task": {"type": "string", "minLength": 1, "maxLength": 16000},
            "input": {"type": "object"},
        },
        "required": ["agent_id", "task"],
        "additionalProperties": False,
    },
    "source": "runtime",
}, {
    "name": "send_group_message",
    "description": (
        "WHEN: contribute or direct a message inside the current governed Group. WHEN NOT: do not contact outsiders. "
        "RETURNS: the committed Group message and wake decision. RECOVERY: correct recipient/visibility after policy errors."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "minLength": 1, "maxLength": 16000},
            "visibility": {"type": "string", "enum": ["direct", "group"]},
            "recipient_agent_id": {"type": "string", "minLength": 1, "maxLength": 191},
        },
        "required": ["content", "visibility"],
        "additionalProperties": False,
    },
    "source": "runtime",
    "side_effect": "idempotent",
    "group_only": True,
}, {
    "name": "invite_agent",
    "description": (
        "WHEN: propose an additional governed Group member. WHEN NOT: do not assume invitations are permitted. "
        "RETURNS: an auditable invitation outcome. RECOVERY: fixed-for-run Groups require a new Group instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "minLength": 1, "maxLength": 191},
            "reason": {"type": "string", "minLength": 1, "maxLength": 4000},
        },
        "required": ["agent_id", "reason"],
        "additionalProperties": False,
    },
    "source": "runtime",
    "side_effect": "idempotent",
    "group_only": True,
}, {
    "name": "update_shared_task",
    "description": (
        "WHEN: track optional shared Group work. WHEN NOT: do not use it to schedule an Agent. "
        "RETURNS: the persisted SharedTask. RECOVERY: refresh the task and retry valid state transitions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "title": {"type": "string", "minLength": 1, "maxLength": 300},
            "description": {"type": "string", "maxLength": 16000},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "blocked", "cancelled"]},
            "assignee_agent_id": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "source": "runtime",
    "side_effect": "idempotent",
    "group_only": True,
})

for _definition in RUNTIME_COORDINATION_DEFINITIONS:
    _result_schema = runtime_result_schema(_definition["name"])
    _definition.update({
        "output_schema": _result_schema,
        "registration_hash": hashlib.sha256(json.dumps(
            {
                "name": _definition["name"],
                "description": _definition["description"],
                "input_schema": _definition["input_schema"],
                "output_schema": _result_schema,
                "side_effect": _definition.get("side_effect", "none"),
                "runtime_contract_version": 4,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest(),
        "execution_mode": "inline",
        "confirmation_policy": "never",
    })
    _definition.setdefault("side_effect", "none")


async def dispatch_coordination_tool(
    name: str, arguments: dict, *, context: RuntimeContext, db,
    run_id: str, work_item_id: str, source_turn_id: str, provider_call_id: str,
) -> tuple[ToolResult, bool, str | None]:
    operation_key = f"turn:{source_turn_id}:call:{provider_call_id}"
    if name == "search_tools":
        capability = await db.scalar(select(CapabilitySnapshot).where(
            CapabilitySnapshot.runner_id == context.runner_id,
        ).order_by(CapabilitySnapshot.created_at.desc()))
        if capability is None:
            return ToolResult(
                status="failed", summary="Capability snapshot is unavailable",
                error="Capability snapshot is unavailable", error_code="capability_snapshot_missing",
                error_category="authorization",
            ), False, None
        query = str(arguments.get("query") or "").casefold().strip()
        tokens = [token for token in query.replace("_", " ").split() if token]
        ranked = []
        for definition in capability.authorized_tool_versions:
            name_value = str(definition.get("name") or "").casefold()
            haystack = " ".join([
                name_value, str(definition.get("description") or "").casefold(),
                str(definition.get("namespace") or "").casefold(),
            ])
            score = (
                sum(3 if token in name_value else 1 for token in tokens if token in haystack)
                if tokens else 1
            )
            if score:
                ranked.append((score, definition))
        ranked.sort(key=lambda item: (-item[0], item[1]["name"]))
        loaded = [dict(item[1]) for item in ranked[:int(arguments.get("limit", 20))]]
        effective = set(capability.effective_tool_keys or [])
        denied_reasons = {
            item.get("capability"): item.get("reason_code")
            for item in (capability.denied_capabilities or [])
        }
        summary = [{
            "name": item["name"], "description": item["description"],
            "tool_version_id": item.get("tool_version_id"),
            "execution_available": item["name"] in effective,
            "blocked_reason": (
                None if item["name"] in effective else denied_reasons.get(item["name"])
            ),
        } for item in loaded]
        return ToolResult(
            status="succeeded", summary=f"Loaded {len(loaded)} authorized ToolVersion(s)",
            data={"tools": summary, "loaded_tool_definitions": loaded},
            content_blocks=[{"type": "json", "value": summary}],
            evidence=[{
                "kind": "capability_snapshot", "id": capability.id,
                "loaded_tool_version_ids": [item.get("tool_version_id") for item in loaded],
            }],
        ), False, None
    if name == "update_plan":
        try:
            view = await update_working_plan(
                db, session_id=context.session_id, run_id=run_id,
                owner_agent_id=context.agent_id, runner_id=context.runner_id,
                source_turn_id=source_turn_id, arguments=arguments,
            )
        except WorkingPlanConflict as error:
            return ToolResult(
                status="failed", summary=str(error), error_code="working_plan_conflict",
                error=str(error), error_category="validation",
            ), False, None
        return ToolResult(
            status="succeeded", summary=f"Working plan revision {view['revision']} saved",
            data={"working_plan": view},
        ), False, None

    if name == "discover_agents":
        cards = await discover_agent_cards(
            db, account_id=context.account_id, actor_agent_id=context.agent_id,
            query=arguments.get("query"),
            domain_tags=list(arguments.get("domain_tags") or []),
            group_enabled=arguments.get("group_enabled"),
            limit=int(arguments.get("limit") or 20),
        )
        return ToolResult(
            status="succeeded",
            summary=f"Discovered {len(cards)} governed Agent candidate(s)",
            data={"candidates": [agent_card_view(card) for card in cards]},
        ), False, None

    if name == "invoke_agent":
        caller_runner = await db.get(DurableRunner, context.runner_id)
        caller_work = await db.get(WorkItem, work_item_id)
        run = await db.get(Run, run_id)
        if caller_runner is None or caller_work is None or run is None:
            return ToolResult(
                status="failed", summary="AgentInvocation runtime graph is unavailable",
                error_code="runtime_context_required",
                error="AgentInvocation runtime graph is unavailable",
                error_category="provider",
            ), False, None
        try:
            invocation = await dispatch_agent_invocation(
                db, operation_key=operation_key, provider_call_id=provider_call_id,
                caller_runner=caller_runner, caller_work=caller_work,
                run=run, arguments=arguments,
            )
        except AgentInvocationRejected as error:
            rejected = await persist_rejected_agent_invocation(
                db, caller_runner=caller_runner, caller_work=caller_work,
                run=run, arguments=arguments, operation_key=operation_key,
                reason=str(error), reason_code=error.code,
            )
            return ToolResult(
                status="failed", summary=str(error),
                data={"agent_invocation_id": rejected.id if rejected else None},
                error_code=error.code, error=str(error),
                error_category="authorization",
            ), False, None
        return ToolResult(
            status="succeeded", summary="Waiting for the invoked Agent",
            data={"agent_invocation_id": invocation.id, "status": invocation.status},
        ), True, "agent"

    if name == "send_group_message":
        try:
            message, target_runner = await send_agent_group_message(
                db, context=context, run_id=run_id, work_item_id=work_item_id,
                content=str(arguments.get("content") or "").strip(),
                recipient_agent_id=arguments.get("recipient_agent_id"),
                visibility=str(arguments.get("visibility") or ""),
                operation_key=operation_key,
            )
        except GroupPolicyError as error:
            return ToolResult(
                status="failed", summary=str(error), error_code=error.code,
                error=str(error), error_category="authorization",
            ), False, None
        return ToolResult(
            status="succeeded", summary="Group message persisted",
            data={
                "message_id": message.id,
                "visibility": message.visibility,
                "recipient_agent_id": message.recipient_agent_id,
                "recipient_woken": target_runner is not None,
            },
        ), False, None

    if name == "invite_agent":
        try:
            result = await reject_fixed_run_invitation(
                db, context=context, run_id=run_id, work_item_id=work_item_id,
                target_agent_id=str(arguments.get("agent_id") or "").strip(),
                reason=str(arguments.get("reason") or "").strip(),
                operation_key=operation_key,
            )
        except GroupPolicyError as error:
            return ToolResult(
                status="failed", summary=str(error), error_code=error.code,
                error=str(error), error_category="authorization",
            ), False, None
        return ToolResult(
            status="failed",
            summary="Invitation rejected by fixed_for_run membership policy",
            data=result,
            error_code="membership_policy_fixed_for_run",
            error="Invitation rejected by fixed_for_run membership policy",
            error_category="authorization",
        ), False, None

    if name == "update_shared_task":
        try:
            task = await update_runtime_shared_task(
                db, context=context, run_id=run_id,
                arguments=arguments, operation_key=operation_key,
            )
        except GroupPolicyError as error:
            return ToolResult(
                status="failed", summary=str(error), error_code=error.code,
                error=str(error), error_category="validation",
            ), False, None
        return ToolResult(
            status="succeeded", summary="SharedTask updated", data={"shared_task": task},
        ), False, None

    if name != "ask_user":
        return ToolResult(
            status="failed", summary="Runtime coordination Tool is unavailable",
            error_code="coordination_tool_unavailable",
            error=f"Runtime coordination Tool '{name}' is unavailable",
            error_category="authorization",
        ), False, None

    existing = await db.scalar(select(Clarification).where(
        Clarification.work_item_id == work_item_id,
        Clarification.idempotency_key == operation_key,
    ))
    if existing is not None:
        return ToolResult(
            status="succeeded", summary="Waiting for user clarification",
            data={"clarification_id": existing.id, "status": "waiting_user"},
        ), existing.status == "open", "clarification" if existing.status == "open" else None
    question = str(arguments["question"]).strip()
    owning_run = await db.get(Run, run_id)
    clarification = Clarification(
        id=uuid7(), work_item_id=work_item_id, run_id=run_id,
        session_id=context.session_id, asked_by_agent_id=context.agent_id,
        target_user_id=context.user_id, question=question, status="open",
        idempotency_key=operation_key, source_turn_id=source_turn_id,
        provider_tool_call_id=provider_call_id,
        deadline_at=owning_run.deadline_at if owning_run else None,
    )
    message = Message(
        id=uuid7(), session_id=context.session_id, user_id=context.user_id,
        scope_type="chat", sender_type="agent", sender_agent_id=context.agent_id,
        recipient_type="session", role="assistant", content=question,
        content_format="markdown", status="committed", run_id=run_id,
        work_item_id=work_item_id, clarification_id=clarification.id,
    )
    db.add_all([clarification, message])
    await db.flush()
    events = ExecutionEventWriter(db)
    created = await events.append(
        session_id=context.session_id, run_id=run_id,
        event_type="message.created",
        event_dedupe_key=f"clarification:{clarification.id}:message",
        actor_type="agent", actor_id=context.agent_id,
        entity_type="message", entity_id=message.id, visibility="user",
        summary="Agent asked for clarification",
        payload={"message_id": message.id, "clarification_id": clarification.id},
        correlation_id=run_id,
    )
    message.caused_by_event_id = created.id
    await events.append(
        session_id=context.session_id, run_id=run_id,
        event_type="clarification.requested",
        event_dedupe_key=f"clarification:{clarification.id}:requested",
        actor_type="agent", actor_id=context.agent_id,
        entity_type="clarification", entity_id=clarification.id,
        entity_version=clarification.version, visibility="user",
        summary="User clarification required",
        payload={
            "clarification_id": clarification.id,
            "work_item_id": work_item_id, "question": question,
        },
        correlation_id=run_id, caused_by_event_id=created.id,
    )
    return ToolResult(
        status="succeeded", summary="Waiting for user clarification",
        data={"clarification_id": clarification.id, "status": "waiting_user"},
    ), True, "clarification"
