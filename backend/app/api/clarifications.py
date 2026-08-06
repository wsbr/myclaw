from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.runs import _expected_version
from app.api.sessions import _idempotency, _owned_session
from app.database import get_db
from app.deps import SessionActor, get_session_actor
from app.models.common import uuid7
from app.models.foundation import UserSession
from app.models.runtime_v1 import Clarification, DurableRunner, Message, Run, WorkItem
from app.persistence.reliable_writes import ExecutionEventWriter
from app.services.runtime_resume_service import (
    clarification_continuation, queue_work_item_resume,
)
from app.schemas.runtime_v1 import ClarificationAnswerRequest, ClarificationResponse

router = APIRouter(tags=["clarifications"])


@router.post("/clarifications/{clarification_id}/answer", response_model=ClarificationResponse)
async def answer_clarification(
    clarification_id: str,
    body: ClarificationAnswerRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    answer = body.answer.strip()
    if not answer:
        raise HTTPException(status_code=422, detail={"code": "answer_required"})
    record, replay = await _idempotency(
        db, actor=actor, operation="answer_clarification", key=idempotency_key,
        request={"clarification_id": clarification_id, "answer": answer},
    )
    if replay and record.response_snapshot:
        return ClarificationResponse.model_validate(record.response_snapshot)

    clarification = await db.scalar(select(Clarification).where(
        Clarification.id == clarification_id,
    ).with_for_update())
    if clarification is None or clarification.target_user_id != actor.account.user_id:
        raise HTTPException(status_code=404, detail="Clarification not found")
    await _owned_session(db, clarification.session_id, actor)
    if clarification.version != _expected_version(if_match):
        raise HTTPException(status_code=409, detail={
            "code": "version_conflict", "current_version": clarification.version,
        })
    if clarification.status != "open":
        raise HTTPException(status_code=409, detail={
            "code": "action_already_resolved", "status": clarification.status,
        })

    await db.scalar(select(UserSession).where(
        UserSession.id == clarification.session_id,
    ).with_for_update())
    run = await db.scalar(select(Run).where(
        Run.id == clarification.run_id,
    ).with_for_update())
    work = await db.scalar(select(WorkItem).where(
        WorkItem.id == clarification.work_item_id,
    ).with_for_update())
    if run is None or work is None or run.status in {"completed", "cancelled", "failed"}:
        raise HTTPException(status_code=409, detail={"code": "run_terminal"})
    if work.status != "waiting_user" or work.active_runner_id is not None:
        raise HTTPException(status_code=409, detail={"code": "work_item_not_waiting_for_user"})

    source_runner = await db.scalar(select(DurableRunner).where(
        DurableRunner.work_item_id == work.id,
    ).order_by(DurableRunner.execution_epoch.desc()).limit(1))
    if source_runner is None:
        raise HTTPException(status_code=409, detail={"code": "resume_checkpoint_unavailable"})

    question_message = await db.scalar(select(Message).where(
        Message.clarification_id == clarification.id,
        Message.role == "assistant",
    ).order_by(Message.created_at).limit(1))
    message = Message(
        id=uuid7(), session_id=run.session_id, user_id=run.user_id,
        scope_type="group" if clarification.group_session_id else "chat",
        group_session_id=clarification.group_session_id,
        group_run_id=work.group_run_id,
        sender_type="user", sender_user_id=run.user_id,
        recipient_type="group_node" if clarification.group_node_id else "session",
        recipient_group_node_id=clarification.group_node_id,
        role="user", content=answer, content_format="markdown", status="committed",
        run_id=run.id, work_item_id=work.id, clarification_id=clarification.id,
        reply_to_message_id=question_message.id if question_message else None,
    )
    clarification.status = "answered"
    clarification.answer_text = answer
    clarification.answered_by_user_id = actor.account.user_id
    clarification.version += 1

    db.add(message)
    await db.flush()
    clarification.answer_message_id = message.id
    events = ExecutionEventWriter(db)
    message_event = await events.append(
        session_id=run.session_id, run_id=run.id,
        group_session_id=clarification.group_session_id, group_run_id=work.group_run_id,
        event_type="message.created",
        event_dedupe_key=f"command:answer_clarification:{record.id}:message",
        actor_type="user", actor_id=run.user_id, entity_type="message", entity_id=message.id,
        visibility="user", summary="Clarification answer created",
        payload={"message_id": message.id, "clarification_id": clarification.id},
        correlation_id=run.id,
    )
    message.caused_by_event_id = message_event.id
    answered = await events.append(
        session_id=run.session_id, run_id=run.id,
        group_session_id=clarification.group_session_id, group_run_id=work.group_run_id,
        event_type="clarification.answered",
        event_dedupe_key=f"command:answer_clarification:{record.id}:answer",
        actor_type="user", actor_id=run.user_id, entity_type="clarification",
        entity_id=clarification.id, entity_version=clarification.version,
        visibility="user", summary="Clarification answered",
        payload={"answer_message_id": message.id, "work_item_id": work.id},
        correlation_id=run.id, caused_by_event_id=message_event.id,
    )
    await queue_work_item_resume(
        db, work=work, run=run, source_runner=source_runner,
        pending_results=clarification_continuation(clarification),
        reason="clarification_answer", caused_by_event_id=answered.id,
        actor_id="clarification-control",
    )
    response = ClarificationResponse.model_validate(clarification)
    record.resource_type, record.resource_id, record.http_status = "clarification", clarification.id, 200
    record.response_snapshot = response.model_dump(mode="json")
    await db.commit()
    return response
