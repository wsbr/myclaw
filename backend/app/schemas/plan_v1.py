from pydantic import BaseModel, Field


class PlanCreateRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=20000)
    predecessor_plan_id: str | None = None


class PlanNodeInput(BaseModel):
    node_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    title: str = Field(min_length=1, max_length=300)
    goal: str = Field(min_length=1, max_length=20000)
    acceptance_criteria: str = Field(min_length=1, max_length=10000)
    assigned_agent_id: str


class PlanDependencyInput(BaseModel):
    predecessor_node_key: str
    successor_node_key: str
    dependency_type: str = "success"


class PlanPatchRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=20000)
    deliverable: str = Field(min_length=1, max_length=10000)
    nodes: list[PlanNodeInput] = Field(min_length=1, max_length=200)
    dependencies: list[PlanDependencyInput] = Field(default_factory=list, max_length=1000)
    expected_graph_revision: int = Field(ge=1)


class PlanConfirmRequest(BaseModel):
    expected_graph_revision: int = Field(ge=1)


class PlanReviseRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=10000)
    expected_graph_revision: int = Field(ge=1)
