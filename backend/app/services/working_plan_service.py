"""Append-only WorkingPlan updates used by every Runtime mode."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.models.common import uuid7
from app.models.runtime_v1 import PlanUpdate, WorkingPlan
from app.persistence.reliable_writes import ExecutionEventWriter


class WorkingPlanConflict(ValueError):
    pass


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def working_plan_view(plan: WorkingPlan, update: PlanUpdate | None = None) -> dict:
    return {
        "id": plan.id,
        "session_id": plan.session_id,
        "run_id": plan.run_id,
        "owner_agent_id": plan.owner_agent_id,
        "summary": plan.summary,
        "revision": plan.revision,
        "status": plan.status,
        "version": plan.version,
        "items": list(update.items or []) if update else [],
        "explanation": update.explanation if update else None,
        "content_hash": update.content_hash if update else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
    }


async def current_working_plan(
    db, *, run_id: str, owner_agent_id: str,
) -> tuple[WorkingPlan | None, PlanUpdate | None]:
    plan = await db.scalar(
        select(WorkingPlan).where(
            WorkingPlan.run_id == run_id,
            WorkingPlan.owner_agent_id == owner_agent_id,
            WorkingPlan.status == "active",
        ).order_by(WorkingPlan.updated_at.desc(), WorkingPlan.id.desc()).limit(1)
    )
    if plan is None:
        return None, None
    update = await db.scalar(
        select(PlanUpdate).where(
            PlanUpdate.working_plan_id == plan.id,
            PlanUpdate.revision == plan.revision,
        )
    )
    return plan, update


async def update_working_plan(
    db, *, session_id: str, run_id: str, owner_agent_id: str,
    runner_id: str, source_turn_id: str, arguments: dict,
) -> dict:
    items = list(arguments.get("items") or [])
    if not items:
        raise WorkingPlanConflict("update_plan requires at least one plan item")
    for item in items:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            raise WorkingPlanConflict("each plan item requires non-empty text")
        if item.get("status") not in {"pending", "in_progress", "completed"}:
            raise WorkingPlanConflict("invalid plan item status")
    plan = await db.scalar(
        select(WorkingPlan).where(
            WorkingPlan.run_id == run_id,
            WorkingPlan.owner_agent_id == owner_agent_id,
            WorkingPlan.status == "active",
        ).order_by(WorkingPlan.updated_at.desc(), WorkingPlan.id.desc())
        .with_for_update().limit(1)
    )
    expected = arguments.get("expected_revision")
    if plan is None:
        if expected not in (None, 0):
            raise WorkingPlanConflict("working plan revision changed")
        plan = WorkingPlan(
            id=uuid7(), session_id=session_id, run_id=run_id,
            owner_agent_id=owner_agent_id, summary="", revision=0,
            status="active",
        )
        db.add(plan)
        await db.flush()
    elif expected is not None and expected != plan.revision:
        raise WorkingPlanConflict("working plan revision changed")
    revision = plan.revision + 1
    explanation = str(arguments.get("explanation") or "").strip() or None
    summary = str(arguments.get("summary") or "").strip()
    if not summary:
        active = [str(item["text"]).strip() for item in items if item["status"] != "completed"]
        summary = (active[0] if active else str(items[-1]["text"]).strip())[:2000]
    payload = {
        "working_plan_id": plan.id, "revision": revision,
        "summary": summary, "items": items, "explanation": explanation,
    }
    update = PlanUpdate(
        id=uuid7(), working_plan_id=plan.id, revision=revision,
        items=items, explanation=explanation, updated_by_runner_id=runner_id,
        source_turn_id=source_turn_id, content_hash=_hash(payload),
    )
    plan.summary = summary
    plan.revision = revision
    plan.version += 1
    db.add(update)
    await db.flush()
    await ExecutionEventWriter(db).append(
        session_id=session_id, run_id=run_id,
        event_type="working_plan.updated",
        event_dedupe_key=f"working_plan:{plan.id}:revision:{revision}",
        actor_type="agent", actor_id=owner_agent_id,
        entity_type="working_plan", entity_id=plan.id,
        entity_version=plan.version, visibility="user",
        summary=f"Working plan updated to revision {revision}",
        payload={
            "working_plan_id": plan.id, "revision": revision,
            "summary": summary, "items": items,
        },
        correlation_id=run_id,
    )
    return working_plan_view(plan, update)
