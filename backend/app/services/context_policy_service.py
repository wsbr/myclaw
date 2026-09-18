"""Controlled context profiles and their shared configuration contract."""

from __future__ import annotations

import math

from jsonschema import Draft202012Validator


# These limits constrain metadata only; they never replace the model/platform
# input limit enforced by the Runtime.
LEGACY_CONTEXT_POLICY_DEFAULTS = {
    "context.standard": {
        "token_budget": 8_000, "max_artifacts": 20,
        "input_target_ratio": 0.8, "web_result_budget_ratio": 0.20,
        "recent_history_messages": 6, "recent_tool_rounds": 2,
        "consumed_tool_result_tokens": 1024,
    },
    "context.concise": {
        "token_budget": 4_000, "max_artifacts": 5,
        "input_target_ratio": 0.5, "web_result_budget_ratio": 0.10,
        "recent_history_messages": 2, "recent_tool_rounds": 1,
        "consumed_tool_result_tokens": 512,
    },
    "context.evidence": {
        "token_budget": 8_000, "max_artifacts": 40,
        "input_target_ratio": 0.8, "web_result_budget_ratio": 0.35,
        "recent_history_messages": 10, "recent_tool_rounds": 3,
        "consumed_tool_result_tokens": 2048,
    },
}

# Independent workload profiles, not increasing resource tiers. Long-running
# work needs a wider recent execution window; evidence work needs larger source
# excerpts, with less conversational history competing for that space.
CONTEXT_POLICY_DEFAULTS = {
    key: dict(parameters) for key, parameters in LEGACY_CONTEXT_POLICY_DEFAULTS.items()
}
CONTEXT_POLICY_DEFAULTS["context.standard"].update(
    recent_history_messages=8, recent_tool_rounds=3,
)
CONTEXT_POLICY_DEFAULTS["context.evidence"].update(
    recent_history_messages=4, recent_tool_rounds=2,
)


class ContextPolicyValidationError(ValueError):
    pass


def context_policy_parameter_schema(policy_key: str) -> dict:
    defaults = CONTEXT_POLICY_DEFAULTS.get(policy_key)
    if defaults is None:
        raise ContextPolicyValidationError("unsupported Context Policy")
    bounds = {
        "token_budget": ("integer", 1, 32_000),
        "max_artifacts": ("integer", 0, 100),
        "input_target_ratio": ("number", 0.1, 0.9),
        "web_result_budget_ratio": ("number", 0.05, 0.5),
        "recent_history_messages": ("integer", 0, 40),
        "recent_tool_rounds": ("integer", 1, 8),
        "consumed_tool_result_tokens": ("integer", 128, 8192),
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            name: {"type": kind, "minimum": minimum, "maximum": maximum,
                   "default": defaults[name]}
            for name, (kind, minimum, maximum) in bounds.items()
        },
        # Optional fields preserve frozen versions published before profiles
        # included ratios and max_artifacts explicitly.
        "additionalProperties": False,
    }


def resolve_context_parameters(
    policy_key: str, parameters: dict | None = None, *, frozen: bool = False,
) -> dict:
    """Fill missing fields without changing the meaning of published versions.

    New publications persist the complete current profile. Older frozen versions
    may omit fields, which must continue to use their original compatibility
    defaults rather than silently adopting newly tuned defaults.
    """

    schema = context_policy_parameter_schema(policy_key)
    value = {} if parameters is None else parameters
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error is not None:
        field = next(iter(error.path), None)
        # Do not reflect submitted values or arbitrary unknown keys in errors.
        location = field if field in schema["properties"] else "parameters"
        raise ContextPolicyValidationError(f"invalid Context Policy {location}")
    if any(not math.isfinite(number) for number in value.values()):
        raise ContextPolicyValidationError("Context Policy parameters must be finite")
    # JSON Schema treats e.g. 1.0 as an integer; normalize it for SQL LIMIT and
    # runtime consumers after schema validation has rejected booleans/strings.
    defaults = LEGACY_CONTEXT_POLICY_DEFAULTS if frozen else CONTEXT_POLICY_DEFAULTS
    resolved = {**defaults[policy_key], **value}
    resolved["token_budget"] = int(resolved["token_budget"])
    resolved["max_artifacts"] = int(resolved["max_artifacts"])
    for name in ("recent_history_messages", "recent_tool_rounds", "consumed_tool_result_tokens"):
        resolved[name] = int(resolved[name])
    return resolved
