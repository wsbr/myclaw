from abc import ABC, abstractmethod
from pydantic import BaseModel
from typing import Any


class ToolResult(BaseModel):
    success: bool
    data: Any = None
    error: str | None = None


class RuntimeContext(BaseModel):
    session_id: str
    runner_id: str
    task_id: str | None = None
    user_id: str
    agent_id: str
    call_chain: list[str] = []
    max_depth: int = 5
    timeout: int = 300
    output_channel: str = "platform_ui"

    model_config = {"arbitrary_types_allowed": True}


class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict

    @abstractmethod
    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult: ...

    def to_llm_definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
