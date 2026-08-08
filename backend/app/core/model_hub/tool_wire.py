"""Map provider-neutral Tool definitions at the model-call boundary."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable


def seal_schema(schema: dict[str, Any], *, require_all: bool = False) -> dict[str, Any]:
    """Close schemas and encode optional OpenAI strict fields as nullable.

    OpenAI strict mode requires every property to be listed in ``required``.
    Provider-neutral optional semantics are retained by making properties that
    were optional accept JSON null on the wire; the Dispatcher removes those
    null placeholders before validating the original immutable ToolVersion.
    """

    sealed = deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                properties = node.get("properties") or {}
                if require_all:
                    originally_required = set(node.get("required") or [])
                    for name, child in list(properties.items()):
                        if name not in originally_required:
                            properties[name] = {
                                "anyOf": [child, {"type": "null"}],
                            }
                    node["required"] = list(properties)
                for child in properties.values():
                    visit(child)
            if "items" in node:
                visit(node["items"])
            for keyword in ("allOf", "anyOf", "oneOf"):
                for child in node.get(keyword) or []:
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(sealed)
    return sealed


def openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project Draft 2020-12 into OpenAI's strict function subset.

    Presence-sensitive conditionals remain authoritative at the Dispatcher.
    They cannot remain on the wire after optional properties are represented
    as required nullable fields, because both mutually-exclusive keys are then
    present. Dropping only those structural keywords avoids an unsatisfiable
    Provider schema without weakening the platform validation boundary.
    """

    projected = deepcopy(schema)

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        node.pop("$schema", None)
        node.pop("default", None)
        node.pop("examples", None)
        for keyword in (
            "allOf", "oneOf", "not", "if", "then", "else",
            "dependentRequired", "dependentSchemas",
        ):
            node.pop(keyword, None)
        if node.get("type") == "object" or "properties" in node:
            properties = node.get("properties") or {}
            originally_required = set(node.get("required") or [])
            for name, child in list(properties.items()):
                visit(child)
                if name not in originally_required:
                    properties[name] = {"anyOf": [child, {"type": "null"}]}
            node["required"] = list(properties)
            node["additionalProperties"] = False
        if isinstance(node.get("items"), dict):
            visit(node["items"])
        for child in node.get("anyOf") or []:
            visit(child)

    visit(projected)
    return projected


def to_openai_tools(
    tools: Iterable[dict[str, Any]], *, strict: bool
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        function = {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": (
                openai_strict_schema(tool.get("input_schema") or {})
                if strict else seal_schema(tool.get("input_schema") or {})
            ),
        }
        if strict:
            function["strict"] = True
        result.append({"type": "function", "function": function})
    return result


def to_anthropic_tools(
    tools: Iterable[dict[str, Any]], *, strict: bool
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        mapped = {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": seal_schema(
                tool.get("input_schema") or {}, require_all=False
            ),
        }
        if strict:
            mapped["strict"] = True
        result.append(mapped)
    return result
