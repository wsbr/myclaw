from pydantic import BaseModel
from datetime import datetime


class SessionCreate(BaseModel):
    agent_id: str
    mode: str = "chat"


class SessionResponse(BaseModel):
    id: str
    agent_id: str
    user_id: str
    mode: str
    title: str | None
    status: str
    fork_from_session_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChatRequest(BaseModel):
    message: str
    channel: str | None = None


class ForkRequest(BaseModel):
    runner_id: str
    span_id: str
    modifications: dict
