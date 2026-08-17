from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class AgentScheduleCreate(BaseModel):
    agent_id: str = Field(min_length=1, max_length=36)
    name: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=16000)
    schedule_type: str = Field(pattern=r"^(once|cron|interval)$")
    run_at: datetime | None = None
    cron_expression: str | None = Field(default=None, max_length=191)
    interval_seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    timezone: str = Field(min_length=1, max_length=100)
    end_at: datetime | None = None
    version_policy: str = Field(
        default="latest_published", pattern=r"^(latest_published|pinned)$"
    )
    pinned_agent_version_id: str | None = Field(default=None, max_length=36)
    overlap_policy: str = Field(default="skip", pattern=r"^(skip|queue_one|allow)$")
    misfire_policy: str = Field(default="fire_once", pattern=r"^(skip|fire_once)$")
    misfire_grace_seconds: int = Field(default=300, ge=0, le=86400)

    model_config = {"extra": "forbid"}

    @field_validator("name", "prompt")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("run_at", "end_at")
    @classmethod
    def require_offset(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("datetime must include a UTC offset")
        return value

    @model_validator(mode="after")
    def validate_shape(self):
        expected = {
            "once": (self.run_at is not None and self.cron_expression is None and self.interval_seconds is None),
            "cron": (self.run_at is None and self.cron_expression is not None and self.interval_seconds is None),
            "interval": (self.run_at is None and self.cron_expression is None and self.interval_seconds is not None),
        }[self.schedule_type]
        if not expected:
            raise ValueError("schedule fields do not match schedule_type")
        if self.version_policy == "latest_published" and self.pinned_agent_version_id:
            raise ValueError("latest_published cannot pin a version")
        return self


class AgentSchedulePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1, max_length=16000)
    schedule_type: str | None = Field(default=None, pattern=r"^(once|cron|interval)$")
    run_at: datetime | None = None
    cron_expression: str | None = Field(default=None, max_length=191)
    interval_seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    end_at: datetime | None = None
    version_policy: str | None = Field(default=None, pattern=r"^(latest_published|pinned)$")
    pinned_agent_version_id: str | None = Field(default=None, max_length=36)
    overlap_policy: str | None = Field(default=None, pattern=r"^(skip|queue_one|allow)$")
    misfire_policy: str | None = Field(default=None, pattern=r"^(skip|fire_once)$")
    misfire_grace_seconds: int | None = Field(default=None, ge=0, le=86400)

    model_config = {"extra": "forbid"}

    @field_validator("name", "prompt")
    @classmethod
    def non_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("run_at", "end_at")
    @classmethod
    def require_offset(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("datetime must include a UTC offset")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class AgentScheduleResponse(BaseModel):
    id: str
    agent_id: str
    agent_name: str
    name: str
    prompt: str
    context_mode: str
    source_session_id: str | None
    schedule_type: str
    cron_expression: str | None
    interval_seconds: int | None
    run_at: datetime | None
    timezone: str
    next_fire_at: datetime | None
    next_fire_local: str | None
    end_at: datetime | None
    version_policy: str
    pinned_agent_version_id: str | None
    overlap_policy: str
    misfire_policy: str
    misfire_grace_seconds: int
    status: str
    total_fire_count: int
    last_scheduled_for: datetime | None
    last_fired_at: datetime | None
    last_error_code: str | None
    last_error_detail: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class AgentScheduleFireResponse(BaseModel):
    id: str
    schedule_id: str
    schedule_name: str
    agent_id: str
    agent_name: str
    trigger_kind: str
    context_mode: str
    scheduled_for: datetime
    scheduled_for_local: str
    status: str
    reason_code: str | None
    agent_version_id: str | None
    session_id: str | None
    run_id: str | None
    runner_id: str | None
    result_preview: str | None
    fired_at: datetime | None
    completed_at: datetime | None
    notification_read_at: datetime | None
    error_code: str | None
    error_detail: str | None
    created_at: datetime
    updated_at: datetime
