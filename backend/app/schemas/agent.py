from pydantic import BaseModel
from datetime import datetime


class AgentCreate(BaseModel):
    name: str
    description: str
    avatar_url: str | None = None
    modes: list[str] = ["chat"]
    model_config_: dict = {}
    system_prompt_blocks: dict = {}
    context_strategy: dict = {"window_type": "sliding", "max_tokens": 128000}
    memory_config: dict = {"short_term": True, "long_term": {"enabled": False}}
    sandbox_config: dict = {"enabled": False}
    supported_channels: list[str] = ["platform_ui"]
    group_config: dict | None = None
    alert_config: dict | None = None

    model_config = {"populate_by_name": True}


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    avatar_url: str | None = None
    modes: list[str] | None = None
    model_config_: dict | None = None
    system_prompt_blocks: dict | None = None
    context_strategy: dict | None = None
    memory_config: dict | None = None
    sandbox_config: dict | None = None
    supported_channels: list[str] | None = None
    group_config: dict | None = None
    alert_config: dict | None = None


class AgentListResponse(BaseModel):
    id: str
    name: str
    description: str
    status: str
    modes: list
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentResponse(AgentListResponse):
    owner_id: str
    avatar_url: str | None
    model_config_: dict
    system_prompt_blocks: dict
    context_strategy: dict
    memory_config: dict
    sandbox_config: dict
    supported_channels: list
    group_config: dict | None
    alert_config: dict | None
    tool_ids: list[str] = []
    skill_ids: list[str] = []
    mcp_server_ids: list[str] = []
    builtin_tools: list[str] = []
