"""Provider-neutral Agent-native turn protocol.

The protocol contains model observations and actions only. Business completion
is deliberately absent: a regular assistant message with no Tool Call ends a
turn naturally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
        }


@dataclass(frozen=True)
class ContentPart:
    type: Literal["text", "file_reference", "artifact_reference", "structured_data"]
    value: Any

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "value": self.value}


@dataclass(frozen=True)
class AssistantMessageItem:
    content_parts: tuple[ContentPart, ...]
    type: Literal["assistant_message"] = "assistant_message"

    @property
    def text(self) -> str:
        return "".join(str(part.value) for part in self.content_parts if part.type == "text")

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "content_parts": [part.as_dict() for part in self.content_parts],
        }


@dataclass(frozen=True)
class ToolCallItem:
    call_id: str
    name: str
    arguments: dict[str, Any]
    index: int
    type: Literal["tool_call"] = "tool_call"

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "call_id": self.call_id,
            "name": self.name,
            "arguments": self.arguments,
            "index": self.index,
        }


TurnItem = AssistantMessageItem | ToolCallItem


@dataclass(frozen=True)
class NormalizedTurnResult:
    items: tuple[TurnItem, ...]
    stop_reason: StopReason
    usage: Usage = field(default_factory=Usage)
    model: str | None = None
    provider_request_id: str | None = None
    provider_state_ref: str | None = None

    @property
    def tool_calls(self) -> tuple[ToolCallItem, ...]:
        return tuple(item for item in self.items if isinstance(item, ToolCallItem))

    @property
    def assistant_messages(self) -> tuple[AssistantMessageItem, ...]:
        return tuple(item for item in self.items if isinstance(item, AssistantMessageItem))

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [item.as_dict() for item in self.items],
            "stop_reason": self.stop_reason.value,
            "usage": self.usage.as_dict(),
            "model": self.model,
            "provider_request_id": self.provider_request_id,
            "provider_state_ref": self.provider_state_ref,
        }


@dataclass(frozen=True)
class ProviderRequest:
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...]
    parameters: dict[str, Any]
    provider_state_ref: str | None = None


@dataclass(frozen=True)
class StreamEvent:
    type: Literal["text_delta", "tool_call", "usage", "done"]
    data: dict[str, Any]


@dataclass(frozen=True)
class ProviderAttemptEvent:
    phase: Literal["started", "payload", "succeeded", "failed"]
    attempt_no: int
    received_payload: bool
    request_hash: str
    result: NormalizedTurnResult | None = None
    error: Exception | None = None


class ProviderError(RuntimeError):
    def __init__(
        self, message: str, *, code: str, retryable: bool = False,
        received_payload: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.received_payload = received_payload


class ProviderSchemaError(ProviderError):
    def __init__(self, message: str, *, received_payload: bool = True) -> None:
        super().__init__(
            message, code="provider_schema_error", retryable=False,
            received_payload=received_payload,
        )


def normalize_stop_reason(value: str | None, *, has_tool_calls: bool = False) -> StopReason:
    normalized = (value or "").lower()
    # Terminal provider failures take precedence over observing a Tool Call.
    # A truncated Tool payload is not a valid action and must never be
    # dispatched as if the provider had completed it.
    if normalized in {"length", "max_tokens", "max_output_tokens"}:
        return StopReason.MAX_TOKENS
    if normalized in {"content_filter", "safety", "blocked"}:
        return StopReason.CONTENT_FILTER
    if normalized in {"error", "failed"}:
        return StopReason.ERROR
    if has_tool_calls or normalized in {"tool_calls", "tool_use", "function_call"}:
        return StopReason.TOOL_USE
    if normalized in {"stop", "end_turn", "end", "completed"}:
        return StopReason.END_TURN
    return StopReason.UNKNOWN
