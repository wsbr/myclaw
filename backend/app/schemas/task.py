from pydantic import BaseModel
from datetime import datetime


class TaskCreate(BaseModel):
    agent_id: str
    title: str | None = None
    trigger_type: str = "immediate"
    cron_expr: str | None = None
    event_type: str | None = None
    input: dict | None = None
    output_channel: str = "platform_ui"


class TaskResponse(BaseModel):
    id: str
    agent_id: str
    user_id: str
    session_id: str | None
    title: str | None
    status: str
    waiting_reason: str | None
    trigger_type: str
    cron_expr: str | None
    output_channel: str
    input_data: dict | None
    output_data: dict | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}
