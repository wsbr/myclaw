"""Runtime-owned coordination Tool definitions and persistence handlers."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.config import settings
from app.core.tool_registry.base import RuntimeContext, ToolResult
from app.core.tool_registry.builtin.search_group_history import SearchGroupHistoryTool
from app.core.tool_registry.schema import runtime_result_schema
from app.models.common import uuid7
from app.models.foundation import ExecutionEvent
from app.models.runtime_v1 import (
    AgentInvocation,
    CapabilitySnapshot,
    DurableRunner,
    Clarification,
    Message,
    Run,
    WorkItem,
)
from app.persistence.reliable_writes import ExecutionEventWriter
from app.services.working_plan_service import WorkingPlanConflict, update_working_plan
from app.services.agent_directory_service import agent_card_view, discover_agent_cards
from app.services.agent_people_service import list_agent_people, resolve_agent_person
from app.services.principal_ref_service import (
    InvalidPrincipalRef, decode_account_ref, encode_account_ref,
)
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
    "name": "find_agent_user",
    "description": (
        "WHEN: the user identifies another Platform person by name or handle but no resolved "
        "mention reference is available. Search only people this Agent may contact. WHEN NOT: "
        "do not enumerate people without a concrete collaboration need and do not guess among "
        "multiple matches. RETURNS: a small candidate list with opaque principal_ref values. "
        "RECOVERY: ask the requesting user to choose when resolution is ambiguous."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "source": "runtime",
}, {
    "name": "ask_user",
    "description": (
        "WHEN: one essential human answer is missing. Omit target_ref to ask the requesting "
        "Session owner; to ask another Platform user, use only an opaque principal_ref from an "
        "explicit resolved @mention or find_agent_user. WHEN NOT: do not ask for discoverable "
        "facts, secrets, passwords, or credentials, and never guess among multiple people. "
        "RETURNS: a durable clarification wait. RECOVERY: continue only after the target user's "
        "committed answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 8000},
            "target_ref": {"type": "string", "minLength": 1, "maxLength": 2000},
            "reason": {"type": "string", "maxLength": 2000},
            "context_summary": {"type": "string", "maxLength": 4000},
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
    "name": "select_group_members",
    "description": (
        "WHEN: during the root normal Chat, the current user request can materially benefit from a Group follow-up. "
        "Select the smallest semantically relevant peer set from the exact candidate Agent catalog in the prompt; "
        "the current Chat Agent is retained automatically as the conversation anchor when eligible. "
        "WHEN NOT: do not use outside root Chat, do not select unknown IDs, and never select every candidate by "
        "default. RETURNS: one durable, immutable full member selection that is applied only after Chat completes. "
        "RECOVERY: if no suitable peer exists, continue the normal Chat without selecting a Group."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "selected_agent_ids": {
                "type": "array", "minItems": 1,
                "maxItems": settings.emergent_group_max_members,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 191},
            },
            "rationale": {"type": "string", "minLength": 1, "maxLength": 4000},
        },
        "required": ["selected_agent_ids", "rationale"],
        "additionalProperties": False,
    },
    "source": "runtime",
    "side_effect": "idempotent",
    "chat_only": True,
}, {
    "name": "invoke_agent",
    "description": (
        "WHEN: delegate or hand off a bounded request to an authorized discovered Agent. In Group, dispatch is "
        "asynchronous: the request is immediately persisted as a human-visible directed @ message, this Turn "
        "continues, its final response is retained as the concise handoff status, and the returned result later "
        "arrives as a new directed @ message in a new Turn. In Chat and "
        "Plan, the caller still durably waits for the result. WHEN NOT: do not invoke unknown IDs, do not fan out "
        "by default, and do not duplicate the returned result with send_group_message. RECOVERY: inspect "
        "authorization or the later directed Agent result."
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
    "name": "search_group_history",
    "description": SearchGroupHistoryTool.description,
    "input_schema": SearchGroupHistoryTool.input_schema,
    "source": "runtime",
    "group_only": True,
}, {
    "name": "send_group_message",
    "description": (
        "WHEN: send a directed @ message to one governed member, or append a concise group-wide history note about "
        "material progress, disagreement, risk, or result. A group-wide note wakes nobody and is not injected into "
        "other members' ambient context; they must retrieve it with search_group_history. WHEN NOT: do not contact "
        "outsiders, do not post detailed working traces group-wide, and do not duplicate an invoke_agent deliverable. "
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

    if name == "find_agent_user":
        if not context.account_id:
            return ToolResult(
                status="failed", summary="A Platform account is required for person search",
                error_code="platform_account_required",
                error="A Platform account is required for person search",
                error_category="authorization",
            ), False, None
        people = await list_agent_people(
            db, tenant_id=context.tenant_id, agent_id=context.agent_id,
            requester_account_id=context.account_id,
            query=str(arguments.get("query") or ""),
            limit=int(arguments.get("limit") or 5), exclude_requester=True,
        )
        candidates = [{
            "principal_ref": encode_account_ref(
                tenant_id=context.tenant_id, agent_id=context.agent_id,
                account_id=item.account_id,
            ),
            "display_name": item.display_name,
            "handle": item.handle,
        } for item in people]
        resolution = "none" if not candidates else "unique" if len(candidates) == 1 else "ambiguous"
        return ToolResult(
            status="succeeded",
            summary=f"Found {len(candidates)} contactable Platform user candidate(s)",
            data={"candidates": candidates, "resolution": resolution},
            content_blocks=[{"type": "json", "value": {
                "candidates": candidates, "resolution": resolution,
            }}],
        ), False, None

    if name == "discover_agents":
        cards = await discover_agent_cards(
            db, tenant_id=context.tenant_id, account_id=context.account_id,
            actor_agent_id=context.agent_id,
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

    if name == "select_group_members":
        runner = await db.get(DurableRunner, context.runner_id)
        work = await db.get(WorkItem, work_item_id)
        run = await db.get(Run, run_id)
        if (
            runner is None or work is None or run is None
            or runner.run_id != run.id or work.run_id != run.id
            or run.mode != "chat" or work.id != run.root_work_item_id
            or work.parent_work_item_id is not None
        ):
            return ToolResult(
                status="failed", summary="Group selection is available only in the root normal Chat",
                error_code="group_selection_chat_root_only",
                error="Group selection is available only in the root normal Chat",
                error_category="authorization",
            ), False, None
        invoked_peer = await db.scalar(select(AgentInvocation.id).where(
            AgentInvocation.run_id == run.id,
            AgentInvocation.caller_work_item_id == work.id,
            AgentInvocation.target_work_item_id.is_not(None),
        ).limit(1))
        if invoked_peer is not None:
            return ToolResult(
                status="failed",
                summary="This Chat already delegated work and cannot also emerge a duplicate Group",
                error_code="group_selection_after_agent_invocation",
                error="This Chat already delegated work and cannot also emerge a duplicate Group",
                error_category="conflict",
            ), False, None
        emergence = dict((runner.input_snapshot or {}).get("group_emergence") or {})
        candidates = list(emergence.get("candidates") or [])
        if emergence.get("status") != "ready" or len(candidates) < 2:
            return ToolResult(
                status="failed", summary="A complete Group candidate catalog is unavailable",
                error_code=str(emergence.get("reason") or "group_candidate_catalog_unavailable"),
                error="A complete Group candidate catalog is unavailable",
                error_category="authorization",
            ), False, None
        requested_ids = [
            str(item).strip() for item in arguments.get("selected_agent_ids") or []
        ]
        if len(requested_ids) != len(set(requested_ids)) or not requested_ids:
            return ToolResult(
                status="failed", summary="Select at least one unique Group peer candidate",
                error_code="invalid_group_selection",
                error="Select at least one unique Group peer candidate",
                error_category="validation",
            ), False, None
        candidates_by_id = {str(item.get("agent_id")): item for item in candidates}
        unknown_ids = [item for item in requested_ids if item not in candidates_by_id]
        if unknown_ids:
            return ToolResult(
                status="failed", summary="Selection contains an Agent outside the frozen candidate catalog",
                error_code="group_candidate_not_available",
                error="Selection contains an Agent outside the frozen candidate catalog",
                error_category="authorization",
                error_details={"unknown_agent_ids": unknown_ids},
            ), False, None
        anchor_id = str(emergence.get("current_chat_agent_id") or "").strip()
        selected_ids = list(requested_ids)
        if anchor_id in candidates_by_id:
            selected_ids = [anchor_id, *(
                item for item in selected_ids if item != anchor_id
            )]
        if len(selected_ids) < 2:
            return ToolResult(
                status="failed", summary="A Group requires the Chat anchor and at least one peer",
                error_code="invalid_group_selection",
                error="A Group requires the Chat anchor and at least one peer",
                error_category="validation",
            ), False, None
        if len(selected_ids) > settings.emergent_group_max_members:
            return ToolResult(
                status="failed", summary="Selected Group exceeds the configured member bound",
                error_code="group_member_limit_exceeded",
                error="Selected Group exceeds the configured member bound",
                error_category="validation",
                error_details={
                    "selected_count": len(selected_ids),
                    "max_members": settings.emergent_group_max_members,
                },
            ), False, None
        rationale = str(arguments.get("rationale") or "").strip()
        event_key = f"group-emergence-selection:{run.id}"
        existing = await db.scalar(select(ExecutionEvent).where(
            ExecutionEvent.session_id == run.session_id,
            ExecutionEvent.event_dedupe_key == event_key,
        ))
        if existing is not None:
            existing_ids = list((existing.payload or {}).get("selected_agent_ids") or [])
            if existing_ids != selected_ids:
                return ToolResult(
                    status="failed", summary="This Chat already has an immutable Group selection",
                    error_code="group_selection_already_recorded",
                    error="This Chat already has an immutable Group selection",
                    error_category="conflict",
                ), False, None
            return ToolResult(
                status="succeeded", summary=f"Reused the {len(existing_ids)}-Agent Group selection",
                data={
                    "selected_agent_ids": existing_ids,
                    "rationale": str((existing.payload or {}).get("rationale") or ""),
                    "candidate_catalog_hash": str(
                        (existing.payload or {}).get("candidate_catalog_hash") or ""
                    ),
                },
            ), False, None
        selected_cards = [candidates_by_id[item] for item in selected_ids]
        candidate_versions = dict(emergence.get("candidate_versions") or {})
        selected_versions = {
            item: dict(candidate_versions.get(item) or {}) for item in selected_ids
        }
        if any(not item.get("agent_version_id") for item in selected_versions.values()):
            return ToolResult(
                status="failed", summary="The frozen Agent version catalog is incomplete",
                error_code="group_candidate_catalog_unavailable",
                error="The frozen Agent version catalog is incomplete",
                error_category="authorization",
            ), False, None
        await ExecutionEventWriter(db).append(
            session_id=run.session_id,
            run_id=run.id,
            event_type="group.emergence_selected",
            event_dedupe_key=event_key,
            actor_type="agent",
            actor_id=runner.agent_id,
            entity_type="run",
            entity_id=run.id,
            entity_version=run.version,
            visibility="user",
            summary=f"Chat model selected {len(selected_ids)} Agents for Group emergence",
            payload={
                "selected_agent_ids": selected_ids,
                "selected_agents": selected_cards,
                "selected_agent_versions": selected_versions,
                "rationale": rationale,
                "candidate_catalog_hash": str(emergence.get("catalog_hash") or ""),
                "candidate_count": len(candidates),
                "source_turn_id": source_turn_id,
                "provider_call_id": provider_call_id,
            },
            correlation_id=run.id,
        )
        return ToolResult(
            status="succeeded", summary=f"Selected {len(selected_ids)} Agents for post-Chat Group emergence",
            data={
                "selected_agent_ids": selected_ids,
                "rationale": rationale,
                "candidate_catalog_hash": str(emergence.get("catalog_hash") or ""),
            },
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
        if run.mode == "group":
            return ToolResult(
                status="succeeded",
                summary=(
                    "Handoff dispatched asynchronously; continue this Turn with a concise status. "
                    "That final status is retained automatically; do not send it again with send_group_message. "
                    "The directed result will arrive in a new Turn."
                ),
                data={
                    "agent_invocation_id": invocation.id,
                    "status": invocation.status,
                    "delivery_mode": "async_handoff",
                },
            ), False, None
        return ToolResult(
            status="succeeded", summary="Waiting for the invoked Agent",
            data={
                "agent_invocation_id": invocation.id,
                "status": invocation.status,
                "delivery_mode": "awaited",
            },
        ), True, "agent"

    if name == "search_group_history":
        result = await SearchGroupHistoryTool().execute(arguments, context, db=db)
        return result, False, None

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
    owning_run = await db.get(Run, run_id)
    if owning_run is None or not context.account_id:
        return ToolResult(
            status="failed", summary="Cross-user clarification authority is unavailable",
            error_code="clarification_authority_unavailable",
            error="Cross-user clarification authority is unavailable",
            error_category="authorization",
        ), False, None
    target_ref = str(arguments.get("target_ref") or "").strip()
    target_account_id = context.account_id
    if target_ref:
        try:
            target_account_id = decode_account_ref(
                target_ref, tenant_id=context.tenant_id, agent_id=context.agent_id,
            )
        except InvalidPrincipalRef as error:
            return ToolResult(
                status="failed", summary=str(error),
                error_code="invalid_platform_user_reference", error=str(error),
                error_category="authorization",
            ), False, None
    target = await resolve_agent_person(
        db, tenant_id=context.tenant_id, agent_id=context.agent_id,
        requester_account_id=context.account_id,
        target_account_id=target_account_id,
    )
    if target is None:
        return ToolResult(
            status="failed", summary="The target user is not contactable by this Agent",
            error_code="platform_user_not_contactable",
            error="The target user is not contactable by this Agent",
            error_category="authorization",
        ), False, None
    question = str(arguments["question"]).strip()
    context_summary = str(arguments.get("context_summary") or "").strip() or None
    clarification = Clarification(
        id=uuid7(), work_item_id=work_item_id, run_id=run_id,
        session_id=context.session_id, asked_by_agent_id=context.agent_id,
        tenant_id=context.tenant_id, requester_account_id=context.account_id,
        target_account_id=target.account_id, target_user_id=target.user_id,
        target_display_name_snapshot=target.display_name,
        target_handle_snapshot=target.handle,
        question=question, context_summary=context_summary, status="open",
        idempotency_key=operation_key, source_turn_id=source_turn_id,
        provider_tool_call_id=provider_call_id,
        deadline_at=owning_run.deadline_at if owning_run else None,
    )
    message = Message(
        id=uuid7(), session_id=context.session_id, user_id=context.user_id,
        scope_type="chat", sender_type="agent", sender_agent_id=context.agent_id,
        recipient_type=(
            "session" if target.account_id == context.account_id else "platform_account"
        ), role="assistant", content=question,
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
        summary=f"Agent asked {target.display_name} for clarification",
        payload={
            "message_id": message.id, "clarification_id": clarification.id,
            "target_account_id": target.account_id,
            "target_display_name": target.display_name,
        },
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
        summary=f"Clarification requested from {target.display_name}",
        payload={
            "clarification_id": clarification.id,
            "work_item_id": work_item_id, "question": question,
            "target_account_id": target.account_id,
            "target_display_name": target.display_name,
            "reason": str(arguments.get("reason") or "").strip() or None,
        },
        correlation_id=run_id, caused_by_event_id=created.id,
    )
    return ToolResult(
        status="succeeded", summary="Waiting for user clarification",
        data={
            "clarification_id": clarification.id, "status": "waiting_user",
            "target_display_name": target.display_name,
        },
    ), True, "clarification"
