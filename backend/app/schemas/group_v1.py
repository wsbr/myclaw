from pydantic import BaseModel, Field


class GroupFormationCreateRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=20000)


class GroupParticipantInput(BaseModel):
    participant_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    agent_id: str
    is_entry: bool = False
    initial_responsibility: str = Field(min_length=1, max_length=10000)


class GroupInitialWorkItemInput(BaseModel):
    work_item_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    participant_key: str
    title: str = Field(min_length=1, max_length=300)
    goal: str = Field(min_length=1, max_length=20000)
    acceptance_criteria: str = Field(min_length=1, max_length=10000)


class GroupSuggestedEdgeInput(BaseModel):
    source_participant_key: str
    target_participant_key: str
    label: str = Field(min_length=1, max_length=300)
    rationale: str = Field(default="", max_length=10000)


class GroupFormationPatchRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=20000)
    bootstrap_context: str = Field(min_length=1, max_length=50000)
    participants: list[GroupParticipantInput] = Field(min_length=1, max_length=50)
    initial_work_items: list[GroupInitialWorkItemInput] = Field(min_length=1, max_length=200)
    suggested_edges: list[GroupSuggestedEdgeInput] = Field(default_factory=list, max_length=500)
    expected_revision_no: int = Field(ge=1)


class GroupFormationConfirmRequest(BaseModel):
    expected_revision_no: int = Field(ge=1)


class GroupRunCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    recipient_node_id: str | None = None
    reference_run_ids: list[str] = Field(default_factory=list, max_length=50)


class GroupRunMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    recipient_node_id: str | None = None


class GroupCloseRequest(BaseModel):
    reason: str = Field(default="User closed Group", min_length=1, max_length=1000)
