from pydantic import BaseModel
from datetime import datetime


class NotificationResponse(BaseModel):
    id: str
    agent_id: str | None
    session_id: str | None
    task_id: str | None
    channel: str
    title: str | None
    content: str
    is_read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UnreadCountResponse(BaseModel):
    count: int
