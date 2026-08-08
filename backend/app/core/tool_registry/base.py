from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.core.tool_registry.schema import TOOL_RESULT_SCHEMA


class ToolError(BaseModel):
    code: str
    message: str
    category: Literal[
        "validation", "authorization", "confirmation", "conflict",
        "rate_limit", "timeout", "transient", "provider", "cancelled",
        "outcome_unknown",
    ]
    details: dict = Field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def __contains__(self, value: str) -> bool:
        return value in self.message


class ToolResult(BaseModel):
    """Safe, structured Tool result envelope.

    The constructor accepts the legacy ``success=`` and string ``error=``
    arguments used by in-process Tool implementations, but serialized results
    always follow the platform contract.
    """

    status: Literal["succeeded", "failed", "cancelled", "outcome_unknown"]
    summary: str
    content_blocks: list[dict[str, Any]] = Field(default_factory=list)
    data: Any = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    provider: dict[str, Any] | None = None
    timing: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False
    error: ToolError | None = None

    def __init__(self, **values):
        legacy_success = values.pop("success", None)
        if "status" not in values:
            values["status"] = "succeeded" if legacy_success is True else "failed"
        raw_error = values.get("error")
        if isinstance(raw_error, str):
            values["error"] = ToolError(
                code=str(values.pop("error_code", "tool_execution_failed")),
                message=raw_error,
                category=values.pop("error_category", "provider"),
                details=values.pop("error_details", {}),
            )
        if values["status"] != "succeeded" and values.get("error") is None:
            category = values.pop(
                "error_category",
                values["status"]
                if values["status"] in {"cancelled", "outcome_unknown"}
                else "provider",
            )
            values["error"] = ToolError(
                code=str(values.pop("error_code", "tool_execution_failed")),
                message={
                    "failed": "Tool execution failed",
                    "cancelled": "Tool execution was cancelled",
                    "outcome_unknown": "Tool execution outcome is unknown",
                }[values["status"]],
                category=category,
                details=values.pop("error_details", {}),
            )
        values.setdefault(
            "summary",
            "Tool completed"
            if values["status"] == "succeeded"
            else values["error"].message,
        )
        super().__init__(**values)

    @model_validator(mode="after")
    def _status_matches_error(self):
        if self.status == "succeeded" and self.error is not None:
            raise ValueError("successful Tool results cannot contain an error")
        if self.status != "succeeded" and self.error is None:
            raise ValueError("non-successful Tool results require a typed error")
        return self

    @property
    def success(self) -> bool:
        return self.status == "succeeded"


class RuntimeContext(BaseModel):
    session_id: str
    runner_id: str
    task_id: str | None = None
    user_id: str
    agent_id: str
    account_id: str | None = None
    permission_mode: str = "direct"
    permission_revision: int = 1
    execution_epoch: int = 1
    authentication_source: str = "web_session"
    call_chain: list[str] = Field(default_factory=list)
    max_depth: int = 5
    timeout: int = 300
    output_channel: str = "platform_ui"
    workspace_id: str | None = None
    workspace_lock_request_id: str | None = None
    workspace_lease_token: str | None = None
    workspace_fencing_token: int | None = None
    tool_invocation_id: str | None = None
    tool_version_id: str | None = None
    tool_connection_revision_id: str | None = None
    group_session_id: str | None = None
    group_run_id: str | None = None
    group_membership_id: str | None = None

    model_config = {"arbitrary_types_allowed": True}


class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict
    output_schema: dict = TOOL_RESULT_SCHEMA

    @abstractmethod
    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult: ...

    def to_llm_definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }
