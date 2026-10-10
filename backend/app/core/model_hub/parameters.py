"""Explicit model-parameter boundary shared by registration and Provider exits.

Model defaults tune inference; messages, capabilities and request routing belong
to Runtime. Provider extensions must be reviewed here before they can reach an
SDK's extra-body merge. Never silently discard an unsupported frozen parameter.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


_INFERENCE_PARAMETERS = frozenset({"temperature", "max_tokens", "max_output_tokens", "extra_body"})
_EMBEDDING_PARAMETERS = frozenset({"dimensions", "encoding_format"})
_RUNTIME_PARAMETERS = frozenset({"model_id", "tool_choice", "response_format"})
_RESERVED_REQUEST_FIELDS = frozenset({
    "system", "messages", "input", "instructions", "tools", "functions",
    "model", "model_id", "tool_choice", "function_call", "response_format",
    "stream", "stream_options", "parallel_tool_calls", "max_completion_tokens",
    "extra_headers", "extra_query", "timeout", "base_url", "endpoint",
})
_PROVIDER_EXTENSION_KEYS = frozenset({"enable_thinking"})


def _validate_parameters(value: Any, *, runtime: bool) -> dict:
    if not isinstance(value, dict):
        raise ValueError("model parameters must be a JSON object")
    allowed = _INFERENCE_PARAMETERS | (_RUNTIME_PARAMETERS if runtime else _EMBEDDING_PARAMETERS)
    for key in value:
        if key not in allowed:
            if key in _RESERVED_REQUEST_FIELDS:
                raise ValueError(f"model parameter {key} is reserved for Runtime")
            raise ValueError("model parameters contain an unsupported parameter")
    if "extra_body" in value:
        extra = value["extra_body"]
        if not isinstance(extra, dict):
            raise ValueError("model parameter extra_body must be a JSON object")
        for key in extra:
            if key not in _PROVIDER_EXTENSION_KEYS:
                if key in _RESERVED_REQUEST_FIELDS or key in _INFERENCE_PARAMETERS:
                    raise ValueError(f"model parameter extra_body.{key} is reserved for Runtime")
                raise ValueError("extra_body only supports the enable_thinking boolean extension")
        if "enable_thinking" in extra and type(extra["enable_thinking"]) is not bool:
            raise ValueError("extra_body.enable_thinking must be a JSON boolean")
    return value


def validate_model_defaults(value: dict | None) -> dict | None:
    """Pydantic-compatible validation; preserve an omitted ModelPatch value."""
    if value is None:
        return None
    return _validate_parameters(value, runtime=False)


def _runtime_error(error: ValueError):
    from app.runtime.protocol import ProviderError

    return ProviderError(
        f"Invalid model parameters: {error}", code="model_parameters_invalid",
        retryable=False, received_payload=False,
    )


def runtime_model_defaults(value: dict) -> dict:
    """Validate old immutable defaults before adding trusted Runtime fields."""
    try:
        return deepcopy(_validate_parameters(value, runtime=False))
    except ValueError as error:
        raise _runtime_error(error) from error


def validate_provider_parameters(value: dict, *, provider: str) -> dict:
    """Fail closed before chat, streaming, or token-count SDK requests."""
    try:
        validated = _validate_parameters(value, runtime=True)
        if provider == "anthropic":
            if validated.get("extra_body"):
                raise ValueError("extra_body extensions are not supported by the Anthropic adapter")
            if validated.get("response_format") is not None:
                raise ValueError("response_format is not supported by the Anthropic adapter")
        return validated
    except ValueError as error:
        raise _runtime_error(error) from error
