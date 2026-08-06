from abc import ABC, abstractmethod
from typing import AsyncIterator
from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


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


class LLMChunk(BaseModel):
    # transport_payload marks that the Provider emitted a wire event even when
    # its Tool arguments are still incomplete and cannot yet be normalized.
    type: str  # "transport_payload" | "text_delta" | "tool_call_delta" | "done"
    content: str | None = None
    tool_call: ToolCall | None = None
    usage: TokenUsage | None = None
    stop_reason: str | None = None
    model: str | None = None
    provider_request_id: str | None = None
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
    async def count_tokens(self, messages: list[dict]) -> int: ...
