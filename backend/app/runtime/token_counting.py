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
    except Exception:
        return RequestTokenCount(
            conservative_request_tokens(messages, tools, parameters),
            "conservative_utf8_structural_fallback", degraded=True,
        )
