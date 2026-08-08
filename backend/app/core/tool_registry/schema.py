"""Draft 2020-12 validation for every model-facing Tool boundary."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, SchemaError


class ToolSchemaDefinitionError(ValueError):
    pass


def assert_valid_schema(schema: dict[str, Any], *, label: str) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ToolSchemaDefinitionError(f"{label} is not valid Draft 2020-12 JSON Schema: {error.message}") from error


def validation_errors(instance: Any, schema: dict[str, Any]) -> list[dict[str, Any]]:
    assert_valid_schema(schema, label="Tool schema")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path))
    return [
        {
            "path": "/" + "/".join(str(part) for part in error.absolute_path),
            "schema_path": "/" + "/".join(str(part) for part in error.absolute_schema_path),
            "validator": error.validator,
            "message": error.message,
        }
        for error in errors
    ]


def validation_error_refs(instance: Any, schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Return non-reflective validation evidence for untrusted Tool output.

    ``jsonschema`` messages may quote the rejected instance. That is useful
    for model-authored arguments, but Provider output must not be copied into
    logs, events, or Inspector error details merely because it was invalid.
    """

    return [
        {
            "path": issue["path"],
            "schema_path": issue["schema_path"],
            "validator": issue["validator"],
        }
        for issue in validation_errors(instance, schema)
    ]


def require_valid(instance: Any, schema: dict[str, Any], *, label: str) -> None:
    issues = validation_errors(instance, schema)
    if issues:
        rendered = "; ".join(
            f"{item['path'] or '/'}: {item['message']}" for item in issues
        )
        raise ValueError(f"{label} failed schema validation: {rendered}")


CONTENT_BLOCK_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["type"],
    "properties": {
        "type": {"type": "string", "enum": ["text", "json", "citation", "image", "artifact_reference"]},
        "text": {"type": "string"},
        "value": {},
        "url": {"type": "string", "format": "uri"},
        "title": {"type": "string"},
        "artifact_id": {"type": "string"},
        "mime_type": {"type": "string"},
    },
    "additionalProperties": False,
}


TOOL_RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "status", "summary", "content_blocks", "data", "evidence",
        "artifact_ids", "provider", "timing", "retryable", "error",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["succeeded", "failed", "cancelled", "outcome_unknown"]},
        "summary": {"type": "string"},
        "content_blocks": {"type": "array", "items": CONTENT_BLOCK_SCHEMA},
        "data": {},
        "evidence": {"type": "array", "items": {"type": "object"}},
        "artifact_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "provider": {"type": ["object", "null"]},
        "timing": {"type": "object"},
        "retryable": {"type": "boolean"},
        "error": {
            "oneOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "required": ["code", "message", "category", "details"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "category": {"type": "string", "enum": [
                            "validation", "authorization", "confirmation", "conflict",
                            "rate_limit", "timeout", "transient", "provider", "cancelled",
                            "outcome_unknown",
                        ]},
                        "details": {"type": "object"},
                    },
                    "additionalProperties": False,
                },
            ]
        },
    },
    "additionalProperties": False,
    "allOf": [
        {
            "if": {
                "properties": {"status": {"const": "succeeded"}},
                "required": ["status"],
            },
            "then": {"properties": {"error": {"type": "null"}}},
            "else": {"properties": {"error": {"type": "object"}}},
        }
    ],
}


def tool_result_schema(data_schema: dict[str, Any]) -> dict[str, Any]:
    """Build a concrete envelope schema for one immutable ToolVersion."""

    assert_valid_schema(data_schema, label="Tool result data schema")
    schema = deepcopy(TOOL_RESULT_SCHEMA)
    schema["properties"]["data"] = {
        "oneOf": [deepcopy(data_schema), {"type": "null"}],
    }
    schema["allOf"] = [*schema.get("allOf", []), {
        "if": {
            "properties": {"status": {"const": "succeeded"}},
            "required": ["status"],
        },
        "then": {"properties": {"data": deepcopy(data_schema)}},
    }]
    return schema


def _object(*required: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "required": list(required),
        "properties": properties or {},
        # Provider-neutral result contracts define stable required fields but
        # may add backward-compatible evidence metadata in later versions.
        "additionalProperties": True,
    }


_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}
_A = {"type": "array"}
_N_S = {"type": ["string", "null"]}
_N_I = {"type": ["integer", "null"]}


BUILTIN_RESULT_DATA_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": _object("path", "items", "next_cursor", "total", properties={
        "path": _S, "items": _A, "next_cursor": _N_I, "total": _I,
    }),
    "search_files": _object("root", "matches", "truncated", "examined_files", properties={
        "root": _S, "matches": _A, "truncated": _B, "examined_files": _I,
    }),
    "read_file": _object("path", "content", "truncated", "next_offset", "size", "checksum", properties={
        "path": _S, "content": _S, "truncated": _B, "next_offset": _N_I,
        "size": _I, "checksum": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }),
    "write_file": _object("path", "size", "checksum", properties={
        "path": _S, "size": _I, "checksum": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "source_artifact_id": _S,
    }),
    "apply_patch": _object("files", properties={"files": _A}),
    "delete_path": _object("path", "restore_reference", "checksum", properties={
        "path": _S, "restore_reference": _S, "checksum": _N_S,
    }),
    "execute": _object("exit_code", "timed_out", "stdout", "stderr", "output_files", properties={
        "exit_code": {"type": ["integer", "null"]}, "timed_out": _B,
        "stdout": _S, "stderr": _S, "output_files": _A,
    }),
    "publish_artifact": _object("artifact", properties={"artifact": {"type": "object"}}),
    "search_group_history": _object("hits", "scanned_through_sequence", "max_sequence", "next_cursor", properties={
        "hits": _A, "scanned_through_sequence": _I, "max_sequence": _I,
        "next_cursor": _N_S,
    }),
    "memory_search": _object("items", "count", "query", properties={
        "items": _A, "count": _I, "query": _S,
    }),
    "memory_propose": _object("candidate_id", "status", "fact_key", "source_message_ids", "observed_head_version", properties={
        "candidate_id": _S, "status": _S, "fact_key": _S,
        "source_message_ids": _A, "observed_head_version": {"type": ["integer", "null"]},
    }),
    "web_search": _object("query", "results", "count", properties={
        "query": _S, "results": _A, "count": _I,
    }),
    "web_fetch": _object("final_url", "mime_type", "text", "checksum", "truncated", properties={
        "final_url": {"type": "string", "format": "uri"}, "mime_type": _S,
        "text": _S, "checksum": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "truncated": _B,
    }),
    "browser_navigate": _object("url", "title", "status_code", "page_revision", "origin", properties={
        "url": {"type": "string", "format": "uri"}, "title": _S,
        "status_code": {"type": ["integer", "null"]}, "page_revision": _I, "origin": _S,
    }),
    "browser_observe": _object("url", "title", "origin", "page_revision", "dom_fingerprint", "accessibility", "elements", properties={
        "url": {"type": "string", "format": "uri"}, "title": _S, "origin": _S,
        "page_revision": _I, "dom_fingerprint": _S, "accessibility": _S, "elements": _A,
    }),
    "browser_interact": _object("action", "url", "origin", "page_revision", "download", properties={
        "action": _S, "url": {"type": "string", "format": "uri"}, "origin": _S,
        "page_revision": _I, "download": {"type": ["object", "null"]},
    }),
    "browser_screenshot": _object("url", "page_revision", "path", "artifact_id", "checksum", properties={
        "url": {"type": "string", "format": "uri"}, "page_revision": _I,
        "path": _S, "artifact_id": _S,
        "checksum": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }),
}


RUNTIME_RESULT_DATA_SCHEMAS: dict[str, dict[str, Any]] = {
    "search_tools": _object("tools", "loaded_tool_definitions", properties={
        "tools": _A, "loaded_tool_definitions": _A,
    }),
    "update_plan": _object("working_plan", properties={"working_plan": {"type": "object"}}),
    "ask_user": _object("clarification_id", "status", properties={
        "clarification_id": _S, "status": _S,
    }),
    "discover_agents": _object("candidates", properties={"candidates": _A}),
    "invoke_agent": _object("agent_invocation_id", properties={
        "agent_invocation_id": {"type": ["string", "null"]}, "status": _S,
    }),
    "send_group_message": _object("message_id", "visibility", "recipient_woken", properties={
        "message_id": _S, "visibility": _S,
        "recipient_agent_id": {"type": ["string", "null"]}, "recipient_woken": _B,
    }),
    "invite_agent": {"type": ["object", "null"]},
    "update_shared_task": _object("shared_task", properties={"shared_task": {"type": "object"}}),
}


def builtin_result_schema(tool_key: str) -> dict[str, Any]:
    data_schema = BUILTIN_RESULT_DATA_SCHEMAS.get(tool_key)
    if data_schema is None:
        raise ToolSchemaDefinitionError(f"Tool {tool_key} has no concrete result data schema")
    return tool_result_schema(data_schema)


def runtime_result_schema(tool_key: str) -> dict[str, Any]:
    data_schema = RUNTIME_RESULT_DATA_SCHEMAS.get(tool_key)
    if data_schema is None:
        raise ToolSchemaDefinitionError(f"Runtime Tool {tool_key} has no concrete result data schema")
    return tool_result_schema(data_schema)
