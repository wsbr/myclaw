from datetime import datetime
from pydantic import BaseModel, Field


class SessionCreateRequest(BaseModel):
    entry_agent_id: str

    model_config = {"extra": "forbid"}


class SessionSandboxResponse(BaseModel):
    id: str
    sandbox_profile_id: str
    profile_config_version: int
    status: str
    root_uri: str
    version: int

    model_config = {"from_attributes": True}


class SessionResponse(BaseModel):
    id: str
    user_id: str
    account_id: str | None = None
    entry_agent_id: str
    status: str
    title: str | None
    origin_type: str
    permission_mode: str = "direct"
    permission_revision: int = 1
    interaction_mode: str = "chat"
    interaction_revision: int = 1
    version: int
    created_at: datetime
    updated_at: datetime
    session_sandbox: SessionSandboxResponse | None = None

    model_config = {"from_attributes": True}


class MessageCreateRequest(BaseModel):
    content: str

    model_config = {"extra": "forbid"}


class EnterPlanRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=16000)

    model_config = {"extra": "forbid"}


class ContinuePlanningRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=16000)
    expected_revision: int = Field(ge=1)

    model_config = {"extra": "forbid"}


class PlanApprovalRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    subject_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    model_config = {"extra": "forbid"}


class AgentInvocationCreateRequest(BaseModel):
    caller_work_item_id: str = Field(min_length=1, max_length=36)
    target_agent_id: str = Field(min_length=1, max_length=36)
    return_route: str = Field(pattern=r"^(caller|direct|group)$")
    content: str = Field(min_length=1, max_length=16000)
    operation_key: str = Field(min_length=1, max_length=191)

    model_config = {"extra": "forbid"}


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    sender_type: str
    content: str
    content_format: str
    status: str
    run_id: str | None
    tool_activity_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class RunResponse(BaseModel):
    id: str
    session_id: str
    mode: str
    status: str = "active"
    wait_reason: str | None
    terminal_reason: str | None = None
    deadline_at: datetime | None = None
    root_work_item_id: str | None = None
    permission_mode: str = "direct"
    cancellation_requested_at: datetime | None
    version: int
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class RunCancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class WorkItemResponse(BaseModel):
    id: str
    run_id: str
    mode: str
    owner_agent_id: str
    agent_version_id: str
    goal: str
    purpose: str = "root_request"
    status: str
    wait_reason: str | None = None
    active_runner_id: str | None
    execution_epoch: int
    attempt_no: int
    result_summary: str | None
    error_code: str | None
    retryable: bool
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RunnerResponse(BaseModel):
    id: str
    work_item_id: str
    run_id: str
    agent_id: str
    agent_version_id: str
    execution_epoch: int
    fencing_token: int = 1
    status: str
    result_message_id: str | None
    capability_snapshot_id: str | None = None
    error_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ToolConfirmationDecisionRequest(BaseModel):
    arguments_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=1000)


class ApprovalDecisionRequest(BaseModel):
    subject_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=2000)


class ToolConfirmationResponse(BaseModel):
    id: str
    tool_invocation_id: str
    session_id: str
    run_id: str
    tool_key: str
    arguments: dict
    arguments_hash: str
    status: str
    policy_version: int
    version: int
    created_at: datetime
    decided_at: datetime | None


class ClarificationAnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=16000)


class ClarificationResponse(BaseModel):
    id: str
    work_item_id: str
    run_id: str
    session_id: str
    group_session_id: str | None
    group_membership_id: str | None
    asked_by_agent_id: str
    target_user_id: str
    question: str
    status: str
    answer_message_id: str | None
    answer_text: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChatCommandResponse(BaseModel):
    message: MessageResponse
    run: RunResponse
    required_actions: list = Field(default_factory=list)
    links: dict[str, str]
