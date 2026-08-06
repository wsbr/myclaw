"""Plan permission-mode subjects and durable approval helpers."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.models.common import uuid7
from app.models.foundation import UserSession
from app.models.runtime_v1 import Approval, PlanUpdate, Run, WorkingPlan, WorkItem
from app.persistence.reliable_writes import ExecutionEventWriter


class PlanSubjectChanged(ValueError):
    pass


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def plan_subject_snapshot(
    *, session: UserSession, run: Run, plan: WorkingPlan, update: PlanUpdate,
) -> dict:
    """Return every mutable authorization input covered by a Plan decision."""
    return {
        "session_id": session.id,
        "run_id": run.id,
        "working_plan_id": plan.id,
        "plan_revision": plan.revision,
        "plan_content_hash": update.content_hash,
        "permission_mode": session.permission_mode,
        "permission_revision": session.permission_revision,
        "target_permission_mode": "direct",
    }


def plan_subject_hash(
    *, session: UserSession, run: Run, plan: WorkingPlan, update: PlanUpdate,
) -> str:
    return _hash(plan_subject_snapshot(
        session=session, run=run, plan=plan, update=update,
    ))


async def create_plan_approval(
    db, *, session: UserSession, run: Run, work: WorkItem,
    plan: WorkingPlan, update: PlanUpdate,
) -> Approval:
    if session.permission_mode != "plan_readonly" or run.mode != "plan":
        raise PlanSubjectChanged("Plan implementation approval requires plan_readonly")
    if session.account_id is None:
        raise PlanSubjectChanged("Plan Session has no internal approval account")
    snapshot = plan_subject_snapshot(
        session=session, run=run, plan=plan, update=update,
    )
    subject_hash = _hash(snapshot)
    existing = await db.scalar(select(Approval).where(
        Approval.kind == "plan_implementation",
        Approval.subject_type == "working_plan",
        Approval.subject_id == plan.id,
        Approval.subject_hash == subject_hash,
    ))
    if existing is not None:
        return existing
    approval = Approval(
        id=uuid7(), session_id=session.id, run_id=run.id,
        work_item_id=work.id, requested_by_agent_id=plan.owner_agent_id,
        kind="plan_implementation", subject_type="working_plan",
        subject_id=plan.id, subject_version=plan.revision,
        target_account_id=session.account_id, subject_hash=subject_hash,
        request_hash=subject_hash, subject_snapshot=snapshot,
        idempotency_key=(
            f"plan:{plan.id}:revision:{plan.revision}:"
            f"permission:{session.permission_revision}"
        ),
        presentation={
            "title": "Approve current plan for implementation",
            "summary": plan.summary,
            "working_plan_id": plan.id,
            "plan_revision": plan.revision,
            "permission_revision": session.permission_revision,
            "target_permission_mode": "direct",
        },
        status="pending",
    )
    db.add(approval)
    await db.flush()
    await ExecutionEventWriter(db).append(
        session_id=session.id, run_id=run.id,
        event_type="approval.requested",
        event_dedupe_key=f"approval:{approval.id}:requested",
        actor_type="agent", actor_id=plan.owner_agent_id,
        entity_type="approval", entity_id=approval.id,
        entity_version=approval.version, visibility="user",
        summary=f"Plan revision {plan.revision} is ready for approval",
        payload={
            "approval_id": approval.id,
            "type": "plan_implementation",
            "working_plan_id": plan.id,
            "plan_revision": plan.revision,
            "permission_revision": session.permission_revision,
            "subject_hash": subject_hash,
        },
        correlation_id=run.id,
    )
    return approval


async def load_current_plan_subject(
    db, *, approval: Approval, lock: bool = False,
) -> tuple[UserSession, Run, WorkItem, WorkingPlan, PlanUpdate]:
    session_query = select(UserSession).where(UserSession.id == approval.session_id)
    run_query = select(Run).where(Run.id == approval.run_id)
    work_query = select(WorkItem).where(WorkItem.id == approval.work_item_id)
    plan_query = select(WorkingPlan).where(WorkingPlan.id == approval.subject_id)
    if lock:
        session_query = session_query.with_for_update()
        run_query = run_query.with_for_update()
        work_query = work_query.with_for_update()
        plan_query = plan_query.with_for_update()
    session = await db.scalar(session_query)
    run = await db.scalar(run_query)
    work = await db.scalar(work_query)
    plan = await db.scalar(plan_query)
    if session is None or run is None or work is None or plan is None:
        raise PlanSubjectChanged("Plan approval subject no longer exists")
    update = await db.scalar(select(PlanUpdate).where(
        PlanUpdate.working_plan_id == plan.id,
        PlanUpdate.revision == plan.revision,
    ))
    if update is None:
        raise PlanSubjectChanged("Current PlanUpdate is unavailable")
    return session, run, work, plan, update


async def assert_current_plan_subject(db, *, approval: Approval, lock: bool = False):
    values = await load_current_plan_subject(db, approval=approval, lock=lock)
    session, run, _work, plan, update = values
    expected_snapshot = plan_subject_snapshot(
        session=session, run=run, plan=plan, update=update,
    )
    if (
        approval.status != "pending"
        or approval.subject_version != plan.revision
        or approval.subject_hash != _hash(expected_snapshot)
        or approval.subject_snapshot != expected_snapshot
    ):
        raise PlanSubjectChanged("Plan revision or permission revision changed")
    return values
