from pydantic import BaseModel, Field


class PlanStepSchema(BaseModel):
    id: str
    description: str
    tool: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    status: str = "pending"
    output: str | None = None


class PlanPreview(BaseModel):
    plan_id: str
    session_id: str
    goal: str
    steps: list[PlanStepSchema]
    status: str


class PlanGenerateRequest(BaseModel):
    goal: str


class PlanUpdateRequest(BaseModel):
    steps: list[PlanStepSchema]


class PlanReviseRequest(BaseModel):
    instruction: str


class PlanExecuteResponse(BaseModel):
    task_id: str
    plan_id: str
