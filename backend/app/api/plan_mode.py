from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.required_actions import approval_view
from app.api.runs import _expected_version
from app.api.sessions import _idempotency, _owned_session
from app.database import get_db
from app.deps import SessionActor, get_session_actor
from app.models.common import utcnow, uuid7
from app.models.foundation import UserSession
from app.models.runtime_v1 import (
    AgentInvocation, Approval, Clarification, DurableRunner, Message,
    Run, ToolInvocation, WorkingPlan, WorkItem,
)
from app.persistence.reliable_writes import ExecutionEventWriter
from app.schemas.runtime_v1 import (
    ContinuePlanningRequest, EnterPlanRequest, MessageResponse,
    PlanApprovalRequest, RunResponse, SessionResponse,
)
from app.services.chat_service import AgentUnavailable, enqueue_chat_message
from app.services.plan_mode_service import (
    PlanSubjectChanged, assert_current_plan_subject,
)
from app.services.runtime_resume_service import queue_work_item_resume


router = APIRouter(tags=["plan-mode"])
TERMINAL_RUN = {"completed", "cancelled", "failed"}
TERMINAL_WORK = {"succeeded", "failed", "timed_out", "cancelled", "abandoned", "blocked"}


def _run_envelope(message: Message | None, run: Run, session: UserSession) -> dict:
    value = {
        "run": RunResponse.model_validate(run).model_dump(mode="json"),
        "required_actions": [],
        "links": {"events": f"/api/v1/sessions/{session.id}/events/stream"},
        "session": SessionResponse.model_validate(session).model_dump(mode="json"),
    }
    if message is not None:
        value["message"] = MessageResponse.model_validate(message).model_dump(mode="json")
    return value


@router.post("/sessions/{session_id}/plan-mode", status_code=202)
async def enter_plan_mode(
    session_id: str, body: EnterPlanRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    session = await _owned_session(db, session_id, actor)
    record, replay = await _idempotency(
        db, actor=actor, operation="enter_plan_mode", key=idempotency_key,
        request={"session_id": session_id, "goal": body.goal},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    session = await db.scalar(select(UserSession).where(
        UserSession.id == session.id,
    ).with_for_update())
    if session.version != _expected_version(if_match):
        raise HTTPException(status_code=409, detail={
            "code": "version_conflict", "current_version": session.version,
        })
    if session.status != "active":
        raise HTTPException(status_code=409, detail={"code": "session_not_active"})
    if session.permission_mode == "plan_readonly":
        raise HTTPException(status_code=409, detail={"code": "plan_mode_already_active"})
    open_run = await db.scalar(select(Run.id).where(
        Run.session_id == session.id,
        Run.status.not_in(tuple(TERMINAL_RUN)),
    ).limit(1))
    if open_run is not None:
        raise HTTPException(status_code=409, detail={"code": "session_run_active"})
    session.interaction_mode = "plan"
    session.interaction_revision += 1
    session.permission_mode = "plan_readonly"
    session.permission_revision += 1
    session.version += 1
    try:
        message, run, _work, _runner = await enqueue_chat_message(
            db, session_id=session.id, user_id=session.user_id,
            content=body.goal, idempotency_record_id=record.id, mode="plan",
        )
    except (ValueError, RuntimeError, AgentUnavailable) as error:
        await db.rollback()
        raise HTTPException(status_code=409, detail={
            "code": "plan_command_rejected", "message": str(error),
        })
    await ExecutionEventWriter(db).append(
        session_id=session.id, run_id=run.id,
        event_type="plan.permission_entered",
        event_dedupe_key=f"command:enter_plan_mode:{record.id}",
        actor_type="user", actor_id=session.user_id,
        entity_type="user_session", entity_id=session.id,
        entity_version=session.version, visibility="user",
        summary="Plan read-only permission mode entered",
        payload={
            "interaction_mode": session.interaction_mode,
            "interaction_revision": session.interaction_revision,
            "permission_mode": session.permission_mode,
            "permission_revision": session.permission_revision,
        },
        correlation_id=run.id,
    )
    response = _run_envelope(message, run, session)
    record.resource_type, record.resource_id, record.http_status = "run", run.id, 202
    record.response_snapshot = response
    await db.commit()
    return response


async def _cancel_open_plan_runs(db, *, session: UserSession, reason: str) -> None:
    runs = list(await db.scalars(select(Run).where(
        Run.session_id == session.id,
        Run.mode == "plan",
        Run.status.not_in(tuple(TERMINAL_RUN)),
    ).with_for_update()))
    now = utcnow()
    for run in runs:
        works = list(await db.scalars(select(WorkItem).where(
            WorkItem.run_id == run.id,
        ).with_for_update()))
        for work in works:
            runners = list(await db.scalars(select(DurableRunner).where(
                DurableRunner.work_item_id == work.id,
                DurableRunner.status.in_(("queued", "running")),
            ).with_for_update()))
            for runner in runners:
                runner.status = "cancelled"
                runner.lease_expires_at = None
                runner.ended_at = now
                runner.version += 1
            for model, pending_status, terminal_status in (
                (Approval, "pending", "cancelled"),
                (Clarification, "open", "cancelled"),
            ):
                rows = list(await db.scalars(select(model).where(
                    model.work_item_id == work.id,
                    model.status == pending_status,
                ).with_for_update()))
                for row in rows:
                    row.status = terminal_status
                    if isinstance(row, Approval):
                        row.decided_at = now
                    row.version += 1
            tools = list(await db.scalars(select(ToolInvocation).where(
                ToolInvocation.work_item_id == work.id,
                ToolInvocation.status.in_((
                    "created", "waiting_confirmation", "waiting_resource",
                    "queued", "running", "cancel_requested",
                )),
            ).with_for_update()))
            for tool in tools:
                tool.status = "cancelled"
                tool.error_code = "plan_mode_exited"
                tool.version += 1
            if work.status not in TERMINAL_WORK:
                work.status = "cancelled"
                work.wait_reason = None
                work.active_runner_id = None
                work.error_code = "plan_mode_exited"
                work.version += 1
        invocations = list(await db.scalars(select(AgentInvocation).where(
            AgentInvocation.run_id == run.id,
            AgentInvocation.status.in_(("created", "queued", "running")),
        ).with_for_update()))
        for invocation in invocations:
            invocation.status = "cancelled"
            invocation.error_code = "plan_mode_exited"
            invocation.completed_at = now
            invocation.version += 1
        plans = list(await db.scalars(select(WorkingPlan).where(
            WorkingPlan.run_id == run.id, WorkingPlan.status == "active",
        ).with_for_update()))
        for plan in plans:
            plan.status = "closed"
            plan.version += 1
        run.cancellation_requested_at = now
        run.status = "cancelled"
        run.wait_reason = None
        run.terminal_reason = "plan_mode_exited"
        run.error_code = "plan_mode_exited"
        run.result_summary = reason
        run.completed_at = now
        run.finished_at = now
        run.version += 1
        await ExecutionEventWriter(db).append(
            session_id=session.id, run_id=run.id,
            event_type="run.cancelled",
            event_dedupe_key=f"run:{run.id}:cancelled:plan_mode_exit",
            actor_type="user", actor_id=session.user_id,
            entity_type="run", entity_id=run.id, entity_version=run.version,
            visibility="user", summary="Plan Run cancelled while exiting Plan mode",
            payload={"reason": reason}, correlation_id=run.id,
        )


@router.post("/sessions/{session_id}/plan-mode/exit", response_model=SessionResponse)
async def exit_plan_mode(
    session_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    session = await _owned_session(db, session_id, actor)
    record, replay = await _idempotency(
        db, actor=actor, operation="exit_plan_mode", key=idempotency_key,
        request={"session_id": session_id},
    )
    if replay and record.response_snapshot:
        return SessionResponse.model_validate(record.response_snapshot)
    session = await db.scalar(select(UserSession).where(
        UserSession.id == session.id,
    ).with_for_update())
    if session.version != _expected_version(if_match):
        raise HTTPException(status_code=409, detail={
            "code": "version_conflict", "current_version": session.version,
        })
    active_direct_plan = await db.scalar(select(Run.id).where(
        Run.session_id == session.id,
        Run.mode == "plan",
        Run.permission_mode == "direct",
        Run.status.not_in(tuple(TERMINAL_RUN)),
    ).limit(1))
    if active_direct_plan is not None:
        raise HTTPException(
            status_code=409, detail={"code": "plan_implementation_active"},
        )
    exited_without_approval = session.permission_mode == "plan_readonly"
    if session.permission_mode == "plan_readonly":
        await _cancel_open_plan_runs(
            db, session=session, reason="User exited Plan mode without approval",
        )
        session.permission_mode = "direct"
        session.permission_revision += 1
    if session.interaction_mode != "chat":
        session.interaction_mode = "chat"
        session.interaction_revision += 1
    session.version += 1
    await ExecutionEventWriter(db).append(
        session_id=session.id, event_type="plan.permission_exited",
        event_dedupe_key=f"command:exit_plan_mode:{record.id}",
        actor_type="user", actor_id=session.user_id,
        entity_type="user_session", entity_id=session.id,
        entity_version=session.version, visibility="user",
        summary=(
            "Plan mode exited without implementation approval"
            if exited_without_approval else "Plan interaction mode exited"
        ),
        payload={
            "interaction_mode": session.interaction_mode,
            "interaction_revision": session.interaction_revision,
            "permission_mode": session.permission_mode,
            "permission_revision": session.permission_revision,
        },
        correlation_id=session.id,
    )
    response = SessionResponse.model_validate(session)
    record.resource_type, record.resource_id, record.http_status = "user_session", session.id, 200
    record.response_snapshot = response.model_dump(mode="json")
    await db.commit()
    return response


async def _owned_working_plan(
    db: AsyncSession, working_plan_id: str, actor: SessionActor, *, lock: bool = False,
) -> WorkingPlan:
    query = select(WorkingPlan).where(WorkingPlan.id == working_plan_id)
    if lock:
        query = query.with_for_update()
    plan = await db.scalar(query)
    if plan is None:
        raise HTTPException(status_code=404, detail="Working plan not found")
    await _owned_session(db, plan.session_id, actor)
    return plan


@router.post("/working-plans/{working_plan_id}/continue-planning", status_code=202)
async def continue_planning(
    working_plan_id: str, body: ContinuePlanningRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    visible = await _owned_working_plan(db, working_plan_id, actor)
    record, replay = await _idempotency(
        db, actor=actor, operation="continue_planning", key=idempotency_key,
        request={
            "working_plan_id": working_plan_id,
            "instruction": body.instruction,
            "expected_revision": body.expected_revision,
        },
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    session = await db.scalar(select(UserSession).where(
        UserSession.id == visible.session_id,
    ).with_for_update())
    run = await db.scalar(select(Run).where(Run.id == visible.run_id).with_for_update())
    plan = await db.scalar(select(WorkingPlan).where(
        WorkingPlan.id == visible.id,
    ).with_for_update())
    work = await db.scalar(select(WorkItem).where(
        WorkItem.id == run.root_work_item_id,
    ).with_for_update())
    if plan.version != _expected_version(if_match) or plan.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail={
            "code": "version_conflict", "current_version": plan.version,
            "current_revision": plan.revision,
        })
    if (
        session.permission_mode != "plan_readonly" or run.mode != "plan"
        or run.status not in {"waiting_approval", "waiting_user"}
        or work is None or work.active_runner_id is not None
    ):
        raise HTTPException(status_code=409, detail={"code": "plan_not_waiting_for_feedback"})
    approvals = list(await db.scalars(select(Approval).where(
        Approval.kind == "plan_implementation",
        Approval.subject_id == plan.id,
        Approval.status == "pending",
    ).with_for_update()))
    now = utcnow()
    events = ExecutionEventWriter(db)
    for approval in approvals:
        approval.status = "cancelled"
        approval.decision_reason = "user_continued_planning"
        approval.decided_at = now
        approval.version += 1
        await events.append(
            session_id=session.id, run_id=run.id,
            event_type="approval.cancelled",
            event_dedupe_key=f"approval:{approval.id}:cancelled:continue",
            actor_type="user", actor_id=session.user_id,
            entity_type="approval", entity_id=approval.id,
            entity_version=approval.version, visibility="user",
            summary="Plan approval cancelled so planning can continue",
            payload={"approval_id": approval.id, "plan_revision": plan.revision},
            correlation_id=run.id,
        )
    clarifications = list(await db.scalars(select(Clarification).where(
        Clarification.work_item_id == work.id,
        Clarification.status == "open",
    ).with_for_update()))
    for clarification in clarifications:
        clarification.status = "cancelled"
        clarification.version += 1
        await events.append(
            session_id=session.id, run_id=run.id,
            event_type="clarification.cancelled",
            event_dedupe_key=f"clarification:{clarification.id}:cancelled:plan-direction",
            actor_type="user", actor_id=session.user_id,
            entity_type="clarification", entity_id=clarification.id,
            entity_version=clarification.version, visibility="user",
            summary="Clarification superseded by a Plan direction change",
            payload={"clarification_id": clarification.id},
            correlation_id=run.id,
        )
    message = Message(
        id=uuid7(), session_id=session.id, user_id=session.user_id,
        scope_type="chat", sender_type="user", sender_user_id=session.user_id,
        recipient_type="session", role="user", content=body.instruction.strip(),
        content_format="markdown", status="committed", run_id=run.id,
        work_item_id=work.id,
    )
    db.add(message)
    await db.flush()
    message_event = await events.append(
        session_id=session.id, run_id=run.id, event_type="message.created",
        event_dedupe_key=f"command:continue_planning:{record.id}:message",
        actor_type="user", actor_id=session.user_id,
        entity_type="message", entity_id=message.id, visibility="user",
        summary="User requested a Plan direction change",
        payload={"message_id": message.id, "working_plan_id": plan.id},
        correlation_id=run.id,
    )
    message.caused_by_event_id = message_event.id
    source_runner = await db.scalar(select(DurableRunner).where(
        DurableRunner.work_item_id == work.id,
    ).order_by(DurableRunner.execution_epoch.desc()).limit(1))
    if source_runner is None:
        raise HTTPException(status_code=409, detail={"code": "resume_checkpoint_unavailable"})
    await queue_work_item_resume(
        db, work=work, run=run, source_runner=source_runner,
        pending_results=[{
            "role": "user",
            "content": (
                "The user did not approve the current plan and requested this change: "
                + body.instruction.strip()
                + " Update the WorkingPlan to a new revision before presenting it again."
            ),
        }],
        reason="continue_planning", caused_by_event_id=message_event.id,
        actor_id="plan-control",
    )
    response = _run_envelope(message, run, session)
    record.resource_type, record.resource_id, record.http_status = "run", run.id, 202
    record.response_snapshot = response
    await db.commit()
    return response


@router.post("/working-plans/{working_plan_id}/approve-implementation")
async def approve_plan_implementation(
    working_plan_id: str, body: PlanApprovalRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    visible = await _owned_working_plan(db, working_plan_id, actor)
    record, replay = await _idempotency(
        db, actor=actor, operation="approve_plan_implementation", key=idempotency_key,
        request={
            "working_plan_id": working_plan_id,
            "expected_revision": body.expected_revision,
            "subject_hash": body.subject_hash,
        },
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    plan = await db.scalar(select(WorkingPlan).where(
        WorkingPlan.id == visible.id,
    ).with_for_update())
    if plan.version != _expected_version(if_match) or plan.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail={
            "code": "approval_subject_changed",
            "current_version": plan.version, "current_revision": plan.revision,
        })
    approval = await db.scalar(select(Approval).where(
        Approval.kind == "plan_implementation",
        Approval.subject_type == "working_plan",
        Approval.subject_id == plan.id,
        Approval.status == "pending",
    ).order_by(Approval.created_at.desc()).with_for_update().limit(1))
    if approval is None or approval.target_account_id != actor.account.id:
        raise HTTPException(status_code=409, detail={"code": "approval_subject_changed"})
    if approval.subject_hash != body.subject_hash:
        raise HTTPException(status_code=409, detail={"code": "approval_subject_changed"})
    try:
        session, run, work, _plan, _update = await assert_current_plan_subject(
            db, approval=approval, lock=True,
        )
    except PlanSubjectChanged:
        raise HTTPException(status_code=409, detail={"code": "approval_subject_changed"})
    if run.status != "waiting_approval" or work.status != "waiting_approval":
        raise HTTPException(status_code=409, detail={"code": "approval_subject_changed"})
    source_runner = await db.scalar(select(DurableRunner).where(
        DurableRunner.work_item_id == work.id,
    ).order_by(DurableRunner.execution_epoch.desc()).limit(1))
    if source_runner is None:
        raise HTTPException(status_code=409, detail={"code": "resume_checkpoint_unavailable"})
    now = utcnow()
    approval.status = "approved"
    approval.decided_by_account_id = actor.account.id
    approval.decided_at = now
    approval.version += 1
    session.permission_mode = "direct"
    session.permission_revision += 1
    session.version += 1
    run.permission_mode = "direct"
    run.version += 1
    decided = await ExecutionEventWriter(db).append(
        session_id=session.id, run_id=run.id,
        event_type="approval.approved",
        event_dedupe_key=f"approval:{approval.id}:approved",
        actor_type="user", actor_id=session.user_id,
        entity_type="approval", entity_id=approval.id,
        entity_version=approval.version, visibility="user",
        summary=f"Plan revision {plan.revision} approved for implementation",
        payload={
            "approval_id": approval.id, "working_plan_id": plan.id,
            "plan_revision": plan.revision,
            "approved_permission_revision": approval.subject_snapshot["permission_revision"],
            "new_permission_revision": session.permission_revision,
        },
        correlation_id=run.id,
    )
    await ExecutionEventWriter(db).append(
        session_id=session.id, run_id=run.id,
        event_type="plan.implementation_approved",
        event_dedupe_key=f"plan:{plan.id}:revision:{plan.revision}:implementation_approved",
        actor_type="user", actor_id=session.user_id,
        entity_type="working_plan", entity_id=plan.id,
        entity_version=plan.version, visibility="user",
        summary="Same Plan Run can now implement with direct permissions",
        payload={
            "working_plan_id": plan.id, "plan_revision": plan.revision,
            "permission_mode": session.permission_mode,
            "permission_revision": session.permission_revision,
        },
        correlation_id=run.id, caused_by_event_id=decided.id,
    )
    await queue_work_item_resume(
        db, work=work, run=run, source_runner=source_runner,
        pending_results=[{
            "role": "user",
            "content": (
                f"The user approved WorkingPlan revision {plan.revision}. "
                "Continue in this same Session and WorkItem, implement dynamically, "
                "and update the WorkingPlan when execution evidence changes."
            ),
        }],
        reason="plan_implementation_approved", caused_by_event_id=decided.id,
        actor_id="plan-control",
        principal_snapshot_updates={
            "permission_revision": session.permission_revision,
            "mode": "plan",
        },
    )
    response = approval_view(approval, None)
    response["session"] = SessionResponse.model_validate(session).model_dump(mode="json")
    response["run"] = RunResponse.model_validate(run).model_dump(mode="json")
    record.resource_type, record.resource_id, record.http_status = "approval", approval.id, 200
    record.response_snapshot = response
    await db.commit()
    return response
