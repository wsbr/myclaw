from pydantic import BaseModel, Field


class GroupCreateRequest(BaseModel):
    """Direct Group created from user-selected Agent Network members."""

    member_agent_ids: list[str] = Field(min_length=3, max_length=3)
    goal: str = Field(min_length=1, max_length=20000)
    reporter_agent_id: str | None = None
    shared_context_refs: list[str] = Field(default_factory=list, max_length=100)


class GroupReporterReplaceRequest(BaseModel):
    reporter_agent_id: str
    clarification_id: str | None = None


class GroupRunCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    recipient_agent_id: str | None = None
    reporter_agent_id: str | None = None
    reference_run_ids: list[str] = Field(default_factory=list, max_length=50)


class GroupRunMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    recipient_agent_id: str | None = None


class SharedTaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20000)
    group_run_id: str | None = None
    assignee_agent_id: str | None = None
    depends_on_task_ids: list[str] = Field(default_factory=list, max_length=100)


class SharedTaskPatchRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20000)
    status: str | None = None
    assignee_agent_id: str | None = None
    depends_on_task_ids: list[str] | None = Field(default=None, max_length=100)
    result_refs: list[dict] | None = Field(default=None, max_length=100)


class GroupCloseRequest(BaseModel):
    reason: str = Field(default="User closed Group", min_length=1, max_length=1000)
