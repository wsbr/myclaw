"""Complete-request token counting with conservative failure fallback."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.model_hub.base import BaseLLM


@dataclass(frozen=True)
class RequestTokenCount:
    tokens: int
    method: str
    degraded: bool = False


@dataclass(frozen=True)
class ModelInputLimits:
    context_window: int
    frozen_max_output_tokens: int
    hard_input_limit: int
    soft_input_target: int


def resolve_model_input_limits(
    capabilities: dict[str, Any] | None,
    parameters: dict[str, Any] | None,
) -> ModelInputLimits:
    values = dict(capabilities or {})
    runtime_parameters = dict(parameters or {})
    context_window = int(values.get("context_window", 24_000))
    raw_frozen_output = runtime_parameters.get(
        "max_output_tokens", runtime_parameters.get("max_tokens", 8192),
    )
    if (
        isinstance(raw_frozen_output, bool)
        or not isinstance(raw_frozen_output, int)
        or raw_frozen_output <= 0
    ):
        raise ValueError("frozen output limit must be a positive integer")
    frozen_output = raw_frozen_output
    declared_output = values.get("max_output_tokens")
    if declared_output is not None and frozen_output > int(declared_output):
        raise ValueError("frozen output exceeds declared model capability")
    context_derived = context_window - frozen_output
    declared_input = values.get("max_input_tokens")
    hard_limit = min(
        context_derived,
        int(declared_input) if declared_input is not None else context_derived,
    )
    if hard_limit <= 0:
        raise ValueError("frozen output leaves no model input capacity")
    return ModelInputLimits(
        context_window=context_window,
        frozen_max_output_tokens=frozen_output,
        hard_input_limit=hard_limit,
        soft_input_target=max(1, int(hard_limit * 0.8)),
    )


def conservative_request_tokens(
    messages: list[dict] | tuple[dict, ...],
    tools: list[dict] | tuple[dict, ...] | None = None,
    parameters: dict[str, Any] | None = None,
) -> int:
    """Upper-biased UTF-8 estimate including JSON/tool structural overhead."""

    relevant_parameters = {
        key: value for key, value in dict(parameters or {}).items()
        if key in {"system", "response_format", "tool_choice", "extra_body"}
    }
    raw = json.dumps({
        "messages": list(messages), "tools": list(tools or ()),
        "parameters": relevant_parameters,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return max(1, (len(raw.encode("utf-8")) + 2) // 3 + 32)


async def count_provider_request(
    provider: BaseLLM, *, provider_type: str,
    messages: list[dict] | tuple[dict, ...],
    tools: list[dict] | tuple[dict, ...] | None,
    parameters: dict[str, Any],
) -> RequestTokenCount:
    normalized_type = provider_type.casefold()
    if normalized_type not in {"anthropic", "openai"}:
        return RequestTokenCount(
            conservative_request_tokens(messages, tools, parameters),
            "conservative_utf8_structural",
        )
    try:
        value = await provider.count_tokens(
            list(messages), list(tools or ()) or None, dict(parameters),
        )
        if int(value) <= 0:
            raise ValueError("provider token count must be positive")
        return RequestTokenCount(
            int(value),
            "anthropic_native" if normalized_type == "anthropic" else "openai_tokenizer",
        )
    except Exception as error:
        if getattr(error, "code", None) == "model_parameters_invalid":
            # A forbidden request override is a configuration failure, not a
            # tokenizer outage that may be hidden by the estimation fallback.
            raise
        return RequestTokenCount(
            conservative_request_tokens(messages, tools, parameters),
            "conservative_utf8_structural_fallback", degraded=True,
        )
