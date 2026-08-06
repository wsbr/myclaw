"""Runtime-owned coordination Tool definitions and persistence handlers."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.core.tool_registry.base import RuntimeContext, ToolResult
from app.models.common import uuid7
from app.models.runtime_v1 import Clarification, Message
from app.persistence.reliable_writes import ExecutionEventWriter
from app.services.working_plan_service import WorkingPlanConflict, update_working_plan


RUNTIME_COORDINATION_DEFINITIONS = ({
    "name": "update_plan",
    "description": (
        "Create or revise your visible working plan when the task benefits from planning. "
        "Do not call this for a simple one-step request."
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
        "Ask one necessary user-facing clarification question and wait durably for the answer."
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
})

for _definition in RUNTIME_COORDINATION_DEFINITIONS:
    _definition.update({
        "output_schema": {"type": "object"},
        "registration_hash": hashlib.sha256(json.dumps(
            {
                "name": _definition["name"],
                "input_schema": _definition["input_schema"],
                "runtime_contract_version": 1,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest(),
        "execution_mode": "inline",
        "side_effect": "none",
        "confirmation_policy": "never",
    })


async def dispatch_coordination_tool(
    name: str, arguments: dict, *, context: RuntimeContext, db,
    run_id: str, work_item_id: str, source_turn_id: str, provider_call_id: str,
) -> tuple[ToolResult, bool, str | None]:
    operation_key = f"turn:{source_turn_id}:call:{provider_call_id}"
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
    clarification = Clarification(
        id=uuid7(), work_item_id=work_item_id, run_id=run_id,
        session_id=context.session_id, asked_by_agent_id=context.agent_id,
        target_user_id=context.user_id, question=question, status="open",
        idempotency_key=operation_key, source_turn_id=source_turn_id,
        provider_tool_call_id=provider_call_id,
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
