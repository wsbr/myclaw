from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.models.session import Session, OrchestrationGraph
from app.services.session_service import SessionService
from app.schemas.session import SessionCreate, SessionResponse, ChatRequest, ForkRequest
from pydantic import BaseModel

router = APIRouter(prefix="/sessions", tags=["sessions"])


class PlanGenerateRequest(BaseModel):
    goal: str


class PlanUpdateRequest(BaseModel):
    steps: list[dict]


class GroupOrchestrateRequest(BaseModel):
    goal: str


class SubstitutionRequest(BaseModel):
    node_id: str
    new_agent_id: str


@router.get("", response_model=list[SessionResponse])
async def list_sessions(
    agent_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await SessionService.list(db, current_user, agent_id=agent_id)


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(
    body: SessionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await SessionService.create(db, current_user, body)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await SessionService.get_or_404(db, session_id, current_user)


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await SessionService.delete(db, session_id, current_user)


@router.post("/{session_id}/chat")
async def chat(
    session_id: str,
    body: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return StreamingResponse(
        SessionService.chat_stream(db, session_id, current_user, body),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{session_id}/fork", response_model=SessionResponse, status_code=201)
async def fork_session(
    session_id: str,
    body: ForkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await SessionService.fork(db, session_id, current_user, body)


@router.get("/{session_id}/runners")
async def list_runners(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await SessionService.list_runners(db, session_id, current_user)


@router.get("/{session_id}/runners/{runner_id}/spans")
async def list_spans(
    session_id: str,
    runner_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await SessionService.list_spans(db, runner_id, current_user)


# ── Plan mode ──────────────────────────────────────────────────────────────

@router.post("/{session_id}/plan/generate")
async def plan_generate(
    session_id: str,
    body: PlanGenerateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.runtime.plan_executor import PlanExecutor
    from app.services.agent_service import AgentService
    session = await SessionService.get_or_404(db, session_id, current_user)
    snapshot = await AgentService.get_snapshot(db, session.agent_id)
    return await PlanExecutor.generate_plan(db, session, snapshot, body.goal)


@router.put("/{session_id}/plan/{plan_id}")
async def plan_update(session_id: str, plan_id: str, body: PlanUpdateRequest):
    return {"plan_id": plan_id, "steps": body.steps, "status": "draft"}


@router.post("/{session_id}/plan/{plan_id}/execute")
async def plan_execute(
    session_id: str,
    plan_id: str,
    body: dict = {},
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.runtime.plan_executor import PlanExecutor
    from app.services.agent_service import AgentService
    session = await SessionService.get_or_404(db, session_id, current_user)
    snapshot = await AgentService.get_snapshot(db, session.agent_id)
    plan = body if body else {}
    task_id = await PlanExecutor.execute_plan(db, session, snapshot, plan)
    return {"task_id": task_id}


# ── Group mode ─────────────────────────────────────────────────────────────

@router.post("/{session_id}/group/orchestrate")
async def group_orchestrate(
    session_id: str,
    body: GroupOrchestrateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.runtime.group_orchestrator import GroupOrchestrator
    from app.services.agent_service import AgentService
    session = await SessionService.get_or_404(db, session_id, current_user)
    snapshot = await AgentService.get_snapshot(db, session.agent_id)
    graph = await GroupOrchestrator.generate_graph(db, session, snapshot, body.goal)
    return graph.graph_json


@router.put("/{session_id}/group/graph")
async def group_update_graph(
    session_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(OrchestrationGraph).where(OrchestrationGraph.session_id == session_id))
    graph = result.scalar_one_or_none()
    if not graph:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Graph not found")
    graph.graph_json = body
    await db.commit()
    return graph.graph_json


@router.post("/{session_id}/group/graph/validate-substitution")
async def validate_substitution(
    session_id: str,
    body: SubstitutionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.runtime.group_orchestrator import GroupOrchestrator
    from app.services.agent_service import AgentService
    session = await SessionService.get_or_404(db, session_id, current_user)
    snapshot = await AgentService.get_snapshot(db, session.agent_id)
    result = await db.execute(select(OrchestrationGraph).where(OrchestrationGraph.session_id == session_id))
    graph = result.scalar_one_or_none()
    return await GroupOrchestrator.validate_substitution(db, snapshot, body.node_id, body.new_agent_id, graph)


@router.post("/{session_id}/group/execute")
async def group_execute(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.runtime.group_orchestrator import GroupOrchestrator
    session = await SessionService.get_or_404(db, session_id, current_user)
    result = await db.execute(select(OrchestrationGraph).where(OrchestrationGraph.session_id == session_id))
    graph = result.scalar_one_or_none()
    if not graph:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="No orchestration graph found for this session")
    graph.status = "confirmed"
    await db.commit()
    task_ids = await GroupOrchestrator.execute_graph(db, session, graph)
    return {"task_ids": task_ids}


@router.get("/{session_id}/group/graph")
async def get_group_graph(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(OrchestrationGraph).where(OrchestrationGraph.session_id == session_id))
    graph = result.scalar_one_or_none()
    if not graph:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Graph not found")
    return {"status": graph.status, "graph": graph.graph_json}
