from abc import ABC, abstractmethod
from typing import AsyncIterator
from pydantic import BaseModel, Field


def bounded_provider_id(value: object) -> str | None:
    """Accept bounded opaque IDs, never arbitrary headers or response bodies."""
    if isinstance(value, str) and 0 < len(value) <= 191 and all(33 <= ord(char) <= 126 for char in value):
        return value
    return None


def provider_request_identity(response: object) -> tuple[str | None, str | None]:
    """Prefer explicit transport request IDs over message/completion IDs."""
    headers = getattr(response, "headers", None)
    if headers is not None:
        for name in ("x-request-id", "request-id", "x-amzn-requestid", "x-amz-request-id"):
            request_id = bounded_provider_id(headers.get(name))
            if request_id:
                return request_id, "header:" + name
    request_id = bounded_provider_id(getattr(response, "request_id", None))
    if request_id:
        return request_id, "sdk:request_id"
    request_id = bounded_provider_id(getattr(response, "_request_id", None))
    return (request_id, "sdk:_request_id") if request_id else (None, None)


class TokenUsage(BaseModel):
    # Absence is not a provider-reported zero (notably for cache counters).
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict


class LLMResponse(BaseModel):
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage
    model: str
    finish_reason: str = "stop"
    provider_request_id: str | None = None
    provider_request_id_source: str | None = None
    provider_response_id: str | None = None


class LLMChunk(BaseModel):
    # transport_payload marks that the Provider has begun an actionable payload
    # (for example buffered Tool JSON) that cannot yet be normalized. Metadata,
    # role-only, reasoning-only, and usage-only wire events must not use it: a
    # transport failure after those events is still safe to retry.
    type: str  # "metadata" | "transport_payload" | "text_delta" | "tool_call_delta" | "done"
    content: str | None = None
    tool_call: ToolCall | None = None
    usage: TokenUsage | None = None
    stop_reason: str | None = None
    model: str | None = None
    provider_request_id: str | None = None
    provider_request_id_source: str | None = None
    provider_response_id: str | None = None
    provider_state_ref: str | None = None
    item_index: int | None = None


class BaseLLM(ABC):
    tool_strict: bool = False

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        params: dict,
    ) -> LLMResponse: ...

    @abstractmethod
    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        params: dict,
    ) -> AsyncIterator[LLMChunk]: ...

    @abstractmethod
    async def count_tokens(
        self, messages: list[dict], tools: list[dict] | None = None,
        params: dict | None = None,
    ) -> int: ...
