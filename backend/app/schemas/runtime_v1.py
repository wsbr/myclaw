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
    version: int
    created_at: datetime
    updated_at: datetime
    session_sandbox: SessionSandboxResponse | None = None

    model_config = {"from_attributes": True}


class MessageCreateRequest(BaseModel):
    content: str

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
    created_at: datetime

    model_config = {"from_attributes": True}


class RunResponse(BaseModel):
    id: str
    session_id: str
    mode: str
    execution_kind: str
    lifecycle: str
    status: str = "active"
    wait_reason: str | None
    outcome: str | None
    entry_agent_id: str
    root_work_item_id: str | None = None
    permission_mode: str = "direct"
    result_summary: str | None
    error_code: str | None
    cancellation_requested_at: datetime | None
    version: int
    started_at: datetime
    finished_at: datetime | None
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
    group_node_id: str | None
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
