from pydantic import BaseModel
from datetime import datetime


class ModelProviderCreate(BaseModel):
    name: str
    provider: str
    base_url: str | None = None
    api_key: str | None = None


class ModelProviderResponse(BaseModel):
    id: str
    name: str
    provider: str
    base_url: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AvailableModelResponse(BaseModel):
    id: str
    model_id: str
    display_name: str
    max_tokens: int
    supports_tools: bool
    supports_streaming: bool
    is_active: bool

    model_config = {"from_attributes": True}
