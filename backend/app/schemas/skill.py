from pydantic import BaseModel
from datetime import datetime


class SkillCreate(BaseModel):
    name: str
    display_name: str
    description: str
    content: str


class SkillUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    content: str | None = None
    is_active: bool | None = None


class SkillResponse(BaseModel):
    id: str
    name: str
    display_name: str
    description: str
    content: str
    version: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
