"""Provider-neutral response contracts for model turns."""

from __future__ import annotations


PLAN_PROPOSAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "goal": {"type": "string", "minLength": 1},
        "deliverable": {"type": "string", "minLength": 1},
        "nodes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "node_key": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "goal": {"type": "string", "minLength": 1},
                    "acceptance_criteria": {"type": "string", "minLength": 1},
                    "assigned_agent_id": {"type": "string", "minLength": 1},
                },
                "required": [
                    "node_key", "title", "goal", "acceptance_criteria", "assigned_agent_id",
                ],
            },
        },
        "dependencies": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "predecessor_node_key": {"type": "string", "minLength": 1},
                    "successor_node_key": {"type": "string", "minLength": 1},
                    "dependency_type": {"type": "string", "enum": ["success", "settled"]},
                },
                "required": [
                    "predecessor_node_key", "successor_node_key", "dependency_type",
                ],
            },
        },
    },
    "required": ["goal", "deliverable", "nodes", "dependencies"],
}


GROUP_FORMATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "goal": {"type": "string", "minLength": 1},
        "bootstrap_context": {"type": "string", "minLength": 1},
        "participants": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "participant_key": {"type": "string", "minLength": 1},
                    "agent_id": {"type": "string", "minLength": 1},
                    "is_entry": {"type": "boolean"},
                    "initial_responsibility": {"type": "string", "minLength": 1},
                },
                "required": [
                    "participant_key", "agent_id", "is_entry", "initial_responsibility",
                ],
            },
        },
        "initial_work_items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "work_item_key": {"type": "string", "minLength": 1},
                    "participant_key": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "goal": {"type": "string", "minLength": 1},
                    "acceptance_criteria": {"type": "string", "minLength": 1},
                },
                "required": [
                    "work_item_key", "participant_key", "title", "goal", "acceptance_criteria",
                ],
            },
        },
        "suggested_edges": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source_participant_key": {"type": "string", "minLength": 1},
                    "target_participant_key": {"type": "string", "minLength": 1},
                    "label": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "source_participant_key", "target_participant_key", "label", "rationale",
                ],
            },
        },
    },
    "required": [
        "goal", "bootstrap_context", "participants", "initial_work_items", "suggested_edges",
    ],
}


CONVERSATION_COMPLETION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "message": {"type": "string"},
        "required_tool_keys": {
            "type": "array",
            "uniqueItems": True,
            "items": {
                "type": "string",
                "enum": [
                    "execute",
                    "invoke_agent",
                    "publish_artifact",
                    "read_file",
                    "request_clarification",
                    "search_group_history",
                    "write_file",
                ],
            },
        },
        "reason_code": {
            "type": "string",
            "enum": [
                "no_observable_effect",
                "clarification_required",
                "delegated_review_required",
                "group_evidence_required",
                "sandbox_execution_required",
                "file_delivery_required",
                "existing_file_required",
                "multiple_effects_required",
            ],
        },
    },
    "required": ["message", "required_tool_keys", "reason_code"],
}


def structured_response_format(contract: str, capability: str) -> dict:
    """Map a declared model capability to an OpenAI-compatible wire contract."""
    if capability == "json_object":
        return {"type": "json_object"}
    if capability != "json_schema":
        raise ValueError(f"unsupported structured output capability: {capability}")
    if contract == "structured.plan_proposal.v1":
        name, schema = "plan_proposal_v1", PLAN_PROPOSAL_SCHEMA
    elif contract == "structured.group_formation.v1":
        name, schema = "group_formation_v1", GROUP_FORMATION_SCHEMA
    elif contract == "structured.conversation_completion.v1":
        name, schema = "conversation_completion_v1", CONVERSATION_COMPLETION_SCHEMA
    else:
        raise ValueError(f"unknown structured response contract: {contract}")
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }
