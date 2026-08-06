from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.sessions import _owned_session
from app.database import get_db
from app.deps import SessionActor, get_session_actor
from app.models.runtime_v1 import PlanUpdate, Run, WorkingPlan
from app.services.working_plan_service import working_plan_view


router = APIRouter(tags=["working-plans"])


@router.get("/runs/{run_id}/working-plans")
async def list_run_working_plans(
    run_id: str, db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    await _owned_session(db, run.session_id, actor)
    plans = list(await db.scalars(
        select(WorkingPlan).where(WorkingPlan.run_id == run_id)
        .order_by(WorkingPlan.created_at, WorkingPlan.id)
    ))
    items = []
    for plan in plans:
        update = await db.scalar(select(PlanUpdate).where(
            PlanUpdate.working_plan_id == plan.id,
            PlanUpdate.revision == plan.revision,
        ))
        items.append(working_plan_view(plan, update))
    return {"items": items, "next_cursor": None}


@router.get("/working-plans/{working_plan_id}")
async def get_working_plan(
    working_plan_id: str, db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    plan = await db.get(WorkingPlan, working_plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Working plan not found")
    await _owned_session(db, plan.session_id, actor)
    update = await db.scalar(select(PlanUpdate).where(
        PlanUpdate.working_plan_id == plan.id,
        PlanUpdate.revision == plan.revision,
    ))
    return working_plan_view(plan, update)


@router.get("/working-plans/{working_plan_id}/updates")
async def list_working_plan_updates(
    working_plan_id: str, db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    plan = await db.get(WorkingPlan, working_plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Working plan not found")
    await _owned_session(db, plan.session_id, actor)
    updates = list(await db.scalars(
        select(PlanUpdate).where(PlanUpdate.working_plan_id == plan.id)
        .order_by(PlanUpdate.revision)
    ))
    return {"items": [{
        "id": item.id, "working_plan_id": item.working_plan_id,
        "revision": item.revision, "items": item.items,
        "explanation": item.explanation, "content_hash": item.content_hash,
        "created_at": item.created_at.isoformat(),
    } for item in updates], "next_cursor": None}
