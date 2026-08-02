from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


class AgentDraftInput(BaseModel):
    name: str = ""
    description: str = ""
    instructions: str = ""
    model_id: str | None = None
    sandbox_profile_id: str | None = None
    context_policy_id: str | None = None
    history_policy_id: str | None = None
    memory_write_policy_id: str | None = None
    memory_recall_policy_id: str | None = None
    group_enabled: bool = False
    tool_ids: list[str] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    bound_agent_ids: list[str] = Field(default_factory=list)


class AgentCreateV1(BaseModel):
    visibility: str = "private"
    draft: AgentDraftInput = Field(default_factory=AgentDraftInput)


class AgentDraftPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    model_id: str | None = None
    sandbox_profile_id: str | None = None
    context_policy_id: str | None = None
    history_policy_id: str | None = None
    memory_write_policy_id: str | None = None
    memory_recall_policy_id: str | None = None
    group_enabled: bool | None = None
    tool_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    bound_agent_ids: list[str] | None = None


class PublishAgentRequest(BaseModel):
    expected_draft_revision: int


class AgentSummaryV1(BaseModel):
    id: str
    owner_account_id: str
    visibility: str
    status: str
    version: int
    draft_version_id: str
    current_published_version_id: str | None
    name: str
    description: str
    group_enabled: bool
    latest_published_version_no: int | None
    created_at: datetime
    updated_at: datetime


class AgentDraftResponse(AgentDraftInput):
    id: str
    agent_id: str
    draft_revision: int
    derived_from_version_id: str | None
    updated_at: datetime


class PublishedVersionResponse(BaseModel):
    id: str
    version_no: int
    publication_status: str = "published"
    content_hash: str
    published_at: datetime


class AgentVersionDetailResponse(AgentDraftInput):
    id: str
    agent_id: str
    version_no: int | None
    publication_status: str
    content_hash: str | None
    derived_from_version_id: str | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AgentDetailV1(BaseModel):
    agent: AgentSummaryV1
    draft: AgentDraftResponse


class AgentPublishResponse(AgentDetailV1):
    published_version: PublishedVersionResponse


class AgentStateCommand(BaseModel):
    reason: str | None = None


class AgentApiKeyCreate(BaseModel):
    name: str
    caller_label: str
    expires_at: datetime | None = None


class AgentApiKeyResponse(BaseModel):
    id: str
    agent_id: str
    name: str
    caller_label: str
    prefix: str
    status: str
    expires_at: datetime | None
    last_used_at: datetime | None
    rotated_from_id: str | None
    version: int
    created_at: datetime
    secret: str | None = None


class DraftTestRunRequest(BaseModel):
    session_id: str | None = None
    content: str
    expected_draft_revision: int
