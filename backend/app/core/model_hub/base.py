from abc import ABC, abstractmethod
from typing import AsyncIterator
from pydantic import BaseModel


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict


class LLMResponse(BaseModel):
    content: str
    tool_calls: list[ToolCall] = []
    usage: TokenUsage
    model: str
    finish_reason: str = "stop"


class LLMChunk(BaseModel):
    type: str                           # "text_delta" | "tool_call_delta" | "done"
    content: str | None = None
    tool_call: ToolCall | None = None
    usage: TokenUsage | None = None


class BaseLLM(ABC):

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
