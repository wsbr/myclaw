from pydantic import BaseModel
from datetime import datetime


class McpServerCreate(BaseModel):
    name: str
    display_name: str
    description: str | None = None
    endpoint: str
    auth_type: str = "none"
    auth_config: dict | None = None
    health_check_url: str | None = None
    capability_summary: str | None = None


class McpServerUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    endpoint: str | None = None
    auth_type: str | None = None
    auth_config: dict | None = None
    health_check_url: str | None = None
    capability_summary: str | None = None
    is_active: bool | None = None


class McpServerResponse(BaseModel):
    id: str
    name: str
    display_name: str
    description: str | None
    endpoint: str
    auth_type: str
    health_check_url: str | None
    capability_summary: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
