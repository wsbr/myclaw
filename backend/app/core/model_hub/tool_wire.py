"""Map provider-neutral Tool definitions at the model-call boundary."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable


def seal_schema(schema: dict[str, Any], *, require_all: bool = False) -> dict[str, Any]:
    """Close object schemas recursively without mutating the registry value."""

    sealed = deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                properties = node.get("properties") or {}
                if require_all:
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


def to_openai_tools(
    tools: Iterable[dict[str, Any]], *, strict: bool
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        function = {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": seal_schema(tool.get("input_schema") or {}, require_all=strict),
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
                tool.get("input_schema") or {}, require_all=strict
            ),
        }
        if strict:
            mapped["strict"] = True
        result.append(mapped)
    return result
