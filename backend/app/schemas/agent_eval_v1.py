from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class AgentEvalCaseInput(BaseModel):
    case_key: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    input_text: str = Field(min_length=1)
    assertions: dict[str, Any]
    weight: Decimal = Field(default=Decimal("1"), gt=0)
    tags: list[str] = Field(default_factory=list, max_length=20)
    source_run_id: str | None = Field(default=None, max_length=36)


class AgentEvalSuiteDefinition(BaseModel):
    expected_version: int | None = Field(default=None, gt=0)
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    gate_policy: dict[str, Any] = Field(default_factory=lambda: {"min_pass_rate": "1"})
    cases: list[AgentEvalCaseInput] = Field(min_length=1, max_length=20)


class AgentEvalRunCreate(BaseModel):
    expected_draft_revision: int = Field(gt=0)


class AgentEvalCaseResponse(BaseModel):
    id: str
    case_key: str
    name: str
    input_text: str
    assertions: dict[str, Any]
    weight: Decimal
    sort_order: int
    tags: list[str] = Field(default_factory=list)
    source_run_id: str | None = None
    source_snapshot: dict[str, Any] | None = None


class AgentEvalSuiteResponse(BaseModel):
    id: str
    agent_id: str
    name: str
    status: str
    version: int
    current_version_id: str
    current_version_no: int
    description: str
    gate_policy: dict[str, Any]
    content_hash: str
    cases: list[AgentEvalCaseResponse]
    created_at: datetime
    updated_at: datetime


class AgentEvalCaseRunResponse(BaseModel):
    id: str
    eval_case_id: str
    case_key: str
    name: str
    session_id: str
    run_id: str
    status: str
    score: Decimal | None
    response_message_id: str | None
    assertion_results: list[dict[str, Any]]
    failure_code: str | None
    started_at: datetime | None
    ended_at: datetime | None
    input_text: str = ""
    response_text: str | None = None
    tags: list[str] = Field(default_factory=list)
    runtime_status: str | None = None


class AgentEvalRunResponse(BaseModel):
    id: str
    agent_id: str
    suite_version_id: str
    suite_name: str
    suite_version_no: int
    candidate_agent_version_id: str
    candidate_content_hash: str
    status: str
    case_count: int
    passed_count: int
    failed_count: int
    error_count: int
    score: Decimal | None
    gate_policy_snapshot: dict[str, Any]
    case_runs: list[AgentEvalCaseRunResponse]
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime
