from pydantic import BaseModel
from datetime import datetime


class ToolCreate(BaseModel):
    name: str
    display_name: str
    description: str
    input_schema: dict
    output_schema: dict | None = None


class ToolUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    is_active: bool | None = None


class ToolResponse(BaseModel):
    id: str
    name: str
    display_name: str
    description: str
    input_schema: dict
    output_schema: dict | None
    is_builtin: bool
    version: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
