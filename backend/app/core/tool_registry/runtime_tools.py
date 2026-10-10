"""Pure contracts for governed runtime coordination tools.

This module contains no execution handlers, database models, configuration, or
runtime imports. Registry reconciliation and runtime authorization share these
contracts without creating an import cycle. A catalog entry is not a grant:
Agent assembly and current ToolGovernance still decide whether it is usable.
"""

from __future__ import annotations

import hashlib
import json

from app.core.tool_registry.schema import runtime_result_schema


SEARCH_GROUP_HISTORY_DESCRIPTION = (
    "WHEN: actively retrieve the human-visible Group transcript, including concise shared notes "
    "and directed @ exchanges between other members, from the current governed Group. "
    "WHEN NOT: never use outside a Group or to access another member's private runtime data. "
    "RETURNS: bounded visible messages, Artifacts, events, sequence bounds, and a stable cursor. "
    "RECOVERY: narrow terms or continue with next_cursor when more evidence remains."
)

SEARCH_GROUP_HISTORY_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 1000},
        "agent_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
        "event_types": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
        "from_sequence": {"type": "integer", "minimum": 1},
        "to_sequence": {"type": "integer", "minimum": 1},
        "cursor": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
    "required": ["query"],
    "additionalProperties": False,
}


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
        "WHEN: this Agent participates in the Group network and needs a governed peer for a bounded subtask. "
        "WHEN NOT: do not use outside the network or for Platform Tools; explicit bound targets are already "
        "provided separately. RETURNS: visible, callable peers that also participate in the Group network. "
        "RECOVERY: refine the query; discovery does not authorize invocation."
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
        "WHEN: during the root normal Chat, this Agent participates in the Group network and the user request "
        "can materially benefit from collaboration. "
        "Select the smallest semantically relevant peer set from the exact candidate Agent catalog in the prompt; "
        "the current Chat Agent is always retained as the conversation anchor and Group member. "
        "WHEN NOT: do not use outside root Chat, do not select unknown IDs, and never select every candidate by "
        "default. RETURNS: one durable, immutable full member selection that is applied only after Chat completes. "
        "RECOVERY: if no suitable peer exists, continue the normal Chat without selecting a Group."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "selected_agent_ids": {
                "type": "array", "minItems": 1,
                "maxItems": 500,
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
        "WHEN: delegate a bounded request to this Agent's explicit bound target, or to an authorized network "
        "peer when both Agents enable Group. Bound delegation may continue across multiple hops, using each "
        "caller's own bindings and the same original-user permissions and Run budget. In Group, dispatch is "
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
    "description": SEARCH_GROUP_HISTORY_DESCRIPTION,
    "input_schema": SEARCH_GROUP_HISTORY_INPUT_SCHEMA,
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

# These references are code-owned identities, not dynamic import targets.
RUNTIME_TOOL_IMPLEMENTATION_REFS = {
    item["name"]: f"app.runtime.coordination_tools:{item['name']}"
    for item in RUNTIME_COORDINATION_DEFINITIONS
}
RUNTIME_PLAN_READONLY_TOOLS = frozenset({"update_plan", "ask_user"})

# Read-only discovery has no business side effect. Planning and clarification
# mutate durable coordination state; plan_readonly permits only the two exact
# trusted contracts above, rather than treating arbitrary annotations as grants.
_RUNTIME_SIDE_EFFECTS = {
    "update_plan": "non_idempotent",
    "ask_user": "idempotent",
    "invoke_agent": "idempotent",
}

for _definition in RUNTIME_COORDINATION_DEFINITIONS:
    _name = _definition["name"]
    _modes = (
        ["group"] if _definition.get("group_only") else
        ["chat"] if _definition.get("chat_only") else ["chat", "plan", "group"]
    )
    _definition.update({
        "output_schema": runtime_result_schema(_name),
        "implementation_ref": RUNTIME_TOOL_IMPLEMENTATION_REFS[_name],
        "implementation_version": "runtime-tools.v5.registry-1",
        "execution_mode": "inline",
        "confirmation_policy": "never",
        "side_effect": _RUNTIME_SIDE_EFFECTS.get(_name, _definition.get("side_effect", "none")),
        "annotations": {
            "runtime_owned": True,
            "runtime_modes": _modes,
            "eager": True,
            **({"runtime_plan_readonly_allowed": True}
               if _name in RUNTIME_PLAN_READONLY_TOOLS else {}),
        },
    })
    _definition["registration_hash"] = hashlib.sha256(json.dumps(
        {
            key: _definition[key] for key in (
                "name", "description", "input_schema", "output_schema",
                "side_effect", "implementation_ref", "implementation_version", "annotations",
            )
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()

RUNTIME_TOOLS = {item["name"]: item for item in RUNTIME_COORDINATION_DEFINITIONS}


def is_runtime_tool_implementation(tool_key: str, implementation_ref: str | None) -> bool:
    """Match both the exact tool key and its code-owned implementation identity."""
    expected = RUNTIME_TOOL_IMPLEMENTATION_REFS.get(tool_key)
    return expected is not None and implementation_ref == expected
