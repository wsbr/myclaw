from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.runs import _expected_version, _idempotency
from app.database import get_db
from app.deps import CurrentAccount, get_current_account
from app.models.common import utcnow, uuid7
from app.models.foundation import UserSession
from app.models.runtime_v1 import Clarification, DurableRunner, Run, ToolConfirmation, ToolInvocation, WorkItem
from app.models.plan_v1 import PlanExecution
from app.models.group_v1 import GroupFormationProposal
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_replacement_runner
from app.schemas.runtime_v1 import ToolConfirmationDecisionRequest, ToolConfirmationResponse
from app.services.cursor_service import decode_cursor, encode_cursor

router = APIRouter(tags=["required-actions"])


def view(confirmation: ToolConfirmation, invocation: ToolInvocation) -> ToolConfirmationResponse:
    return ToolConfirmationResponse(
        id=confirmation.id, tool_invocation_id=invocation.id,
        session_id=invocation.session_id, run_id=invocation.run_id,
        tool_key=confirmation.tool_key, arguments=invocation.arguments,
        arguments_hash=confirmation.arguments_hash, status=confirmation.status,
        policy_version=confirmation.policy_version, version=confirmation.version,
        created_at=confirmation.created_at, decided_at=confirmation.decided_at,
    )


@router.get("/required-actions", response_model=dict)
@router.get("/sessions/{session_id}/required-actions", response_model=dict)
async def list_required_actions(
    session_id: str | None = None, status: str = "pending",
    action_type: str | None = Query(default=None, alias="type"),
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    if session_id is not None:
        session = await db.get(UserSession, session_id)
        if session is None or session.user_id != current.account.user_id:
            raise HTTPException(status_code=404, detail="Session not found")
    allowed_types = {"clarification", "tool_confirmation", "plan_confirmation", "group_confirmation"}
    if action_type is not None and action_type not in allowed_types:
        raise HTTPException(status_code=422, detail={"code": "invalid_required_action_type"})
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
    rows = (await db.execute(
        select(ToolConfirmation, ToolInvocation)
        .join(ToolInvocation, ToolInvocation.id == ToolConfirmation.tool_invocation_id)
        .where(ToolConfirmation.user_id == current.account.user_id, ToolConfirmation.status == status)
        .order_by(ToolConfirmation.created_at, ToolConfirmation.id)
    )).all()
    items = []
    clarification_status = "open" if status == "pending" else status
    clarifications = list(await db.scalars(select(Clarification).where(
        Clarification.target_user_id == current.account.user_id,
        Clarification.status == clarification_status,
    ).order_by(Clarification.created_at, Clarification.id)))
    items.extend({
        "id": item.id, "type": "clarification",
        "status": "pending" if item.status == "open" else item.status,
        "session_id": item.session_id, "run_id": item.run_id,
        "work_item_id": item.work_item_id, "version": item.version,
        "created_at": item.created_at.isoformat(), "expires_at": None,
        "presentation": {"title": "Agent needs your input", "summary": item.question},
        "action": {"method": "POST", "href": f"/api/v1/clarifications/{item.id}/answer",
                   "required_fields": ["answer"]},
        "question": item.question, "asked_by_agent_id": item.asked_by_agent_id,
        "group_session_id": item.group_session_id, "group_node_id": item.group_node_id,
    } for item in clarifications)
    for confirmation, invocation in rows:
        item = view(confirmation, invocation).model_dump(mode="json")
        item["type"] = "tool_confirmation"
        items.append(item)
    plans = (await db.execute(
        select(PlanExecution, Run)
        .join(Run, Run.id == PlanExecution.run_id)
        .where(
            PlanExecution.user_id == current.account.user_id,
            PlanExecution.definition_status == "awaiting_confirmation",
            Run.lifecycle == "waiting", Run.wait_reason == "graph_confirmation",
        )
        .order_by(PlanExecution.created_at, PlanExecution.id)
    )).all()
    items.extend({
        "id": plan.id, "type": "plan_confirmation", "status": "pending",
        "session_id": plan.session_id, "run_id": plan.run_id, "work_item_id": None,
        "version": plan.version, "graph_revision": plan.graph_revision,
        "created_at": plan.created_at.isoformat(), "expires_at": None,
        "presentation": {"title": "Confirm Plan", "summary": plan.goal[:500]},
        "action": {"method": "POST", "href": f"/api/v1/plans/{plan.id}/confirm",
                   "required_fields": ["expected_graph_revision"]},
    } for plan, _ in plans)
    formations = list(await db.scalars(select(GroupFormationProposal).where(
        GroupFormationProposal.user_id == current.account.user_id,
        GroupFormationProposal.status == "awaiting_confirmation",
    ).order_by(GroupFormationProposal.created_at, GroupFormationProposal.id)))
    items.extend({
        "id": proposal.id, "type": "group_confirmation", "status": "pending",
        "session_id": proposal.parent_session_id, "run_id": proposal.generation_run_id,
        "work_item_id": None, "version": proposal.version,
        "revision_no": proposal.current_revision_no, "created_at": proposal.created_at.isoformat(),
        "expires_at": None,
        "presentation": {"title": "Confirm Group Formation", "summary": "Review participants and shared bootstrap context"},
        "action": {"method": "POST", "href": f"/api/v1/group-formations/{proposal.id}/confirm",
                   "required_fields": ["expected_revision_no"]},
    } for proposal in formations)
    if session_id is not None:
        items = [item for item in items if item.get("session_id") == session_id]
    if action_type is not None:
        items = [item for item in items if item.get("type") == action_type]
    items.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""))
    filters = {"session_id": session_id, "status": status, "type": action_type}
    if cursor:
        values = decode_cursor(
            cursor, principal=f"account:{current.account.id}",
            resource="required-actions", filters=filters,
        )
        if len(values) != 2:
            raise HTTPException(status_code=422, detail={"code": "invalid_cursor"})
        boundary = (str(values[0]), str(values[1]))
        items = [item for item in items if (
            item.get("created_at") or "", item.get("id") or "",
        ) > boundary]
    has_more = len(items) > limit
    items = items[:limit]
    next_cursor = None
    if has_more and items:
        next_cursor = encode_cursor(
            principal=f"account:{current.account.id}", resource="required-actions",
            filters=filters,
            values=[items[-1].get("created_at") or "", items[-1].get("id") or ""],
        )
    return {"items": items, "next_cursor": next_cursor}


@router.post("/tool-confirmations/{confirmation_id}/{decision}", response_model=ToolConfirmationResponse)
async def decide_tool_confirmation(
    confirmation_id: str, decision: str, body: ToolConfirmationDecisionRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=404, detail="Decision not found")
    record, replay = await _idempotency(
        db, current, f"{decision}_tool_confirmation", idempotency_key,
        {"confirmation_id": confirmation_id, "reason": body.reason},
    )
    if replay and record.response_snapshot:
        return ToolConfirmationResponse.model_validate(record.response_snapshot)
    confirmation = await db.scalar(select(ToolConfirmation).where(
        ToolConfirmation.id == confirmation_id,
    ).with_for_update())
    if confirmation is None or confirmation.user_id != current.account.user_id:
        raise HTTPException(status_code=404, detail="Tool confirmation not found")
    if confirmation.version != _expected_version(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": confirmation.version})
    if confirmation.status != "pending":
        raise HTTPException(status_code=409, detail={"code": "action_already_resolved", "status": confirmation.status})
    if body.arguments_hash != confirmation.arguments_hash:
        raise HTTPException(status_code=409, detail={"code": "confirmation_arguments_changed"})
    invocation = await db.scalar(select(ToolInvocation).where(
        ToolInvocation.id == confirmation.tool_invocation_id,
    ).with_for_update())
    if (
        invocation.arguments_hash != confirmation.arguments_hash
        or invocation.principal_hash != confirmation.principal_hash
        or invocation.tool_key != confirmation.tool_key
    ):
        raise HTTPException(status_code=409, detail={"code": "confirmation_snapshot_mismatch"})
    run = await db.scalar(select(Run).where(Run.id == invocation.run_id).with_for_update())
    work = await db.scalar(select(WorkItem).where(WorkItem.id == invocation.work_item_id).with_for_update())
    await db.scalar(select(UserSession).where(UserSession.id == invocation.session_id).with_for_update())
    if run.lifecycle in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail={"code": "run_terminal", "lifecycle": run.lifecycle})
    confirmation.status = "approved" if decision == "approve" else "rejected"
    confirmation.decision_by_user_id = current.account.user_id
    confirmation.decision_reason = body.reason
    confirmation.decided_at = utcnow()
    confirmation.version += 1
    invocation.status = "queued" if decision == "approve" else "rejected"
    if decision == "reject":
        invocation.error_code = "tool_rejected_by_user"
        invocation.error_detail = body.reason or "User rejected Tool execution"
    invocation.version += 1
    await db.flush()
    remaining = await db.scalar(
        select(ToolConfirmation.id)
        .join(ToolInvocation, ToolInvocation.id == ToolConfirmation.tool_invocation_id)
        .where(
            ToolInvocation.work_item_id == work.id,
            ToolConfirmation.status == "pending",
        )
        .limit(1)
    )
    if remaining is not None:
        await ExecutionEventWriter(db).append(
            session_id=run.session_id, run_id=run.id,
            event_type=f"tool_confirmation.{confirmation.status}",
            event_dedupe_key=f"command:tool_confirmation:{record.id}:{confirmation.status}",
            actor_type="user", actor_id=run.user_id, entity_type="tool_confirmation",
            entity_id=confirmation.id, entity_version=confirmation.version, visibility="user",
            summary=f"Tool execution {confirmation.status}",
            payload={"tool_key": confirmation.tool_key, "status": confirmation.status},
            correlation_id=run.id,
        )
        response = view(confirmation, invocation)
        record.resource_type, record.resource_id, record.http_status = "tool_confirmation", confirmation.id, 200
        record.response_snapshot = response.model_dump(mode="json")
        await db.commit()
        return response
    work.execution_epoch += 1
    work.attempt_no += 1
    work.status = "queued"
    work.version += 1
    source_runner = await db.get(DurableRunner, invocation.runner_id)
    if source_runner is None or source_runner.work_item_id != work.id:
        raise HTTPException(
            status_code=409,
            detail={"code": "confirmation_source_runner_unavailable"},
        )
    runner = DurableRunner(
        id=uuid7(), work_item_id=work.id, run_id=run.id, session_id=run.session_id,
        agent_id=source_runner.agent_id, agent_version_id=source_runner.agent_version_id,
        execution_epoch=work.execution_epoch, status="queued",
        principal_snapshot=dict(source_runner.principal_snapshot),
        assembly_snapshot=dict(source_runner.assembly_snapshot),
        prompt_manifest=dict(source_runner.prompt_manifest),
        input_snapshot={
            **dict(source_runner.input_snapshot),
            "resumed_from_confirmation_id": confirmation.id,
        },
    )
    run.lifecycle = "running"
    run.wait_reason = None
    run.version += 1
    await persist_replacement_runner(db, work, runner)
    action_event = await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id,
        event_type=f"tool_confirmation.{confirmation.status}",
        event_dedupe_key=f"command:tool_confirmation:{record.id}:{confirmation.status}",
        actor_type="user", actor_id=run.user_id, entity_type="tool_confirmation",
        entity_id=confirmation.id, entity_version=confirmation.version, visibility="user",
        summary=f"Tool execution {confirmation.status}",
        payload={"tool_key": confirmation.tool_key, "status": confirmation.status},
        correlation_id=run.id,
    )
    queued = await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id, event_type="runner.queued",
        event_dedupe_key=f"transition:runner:{runner.id}:0:1:confirmation_resume",
        actor_type="service", actor_id="tool-control", entity_type="runner",
        entity_id=runner.id, entity_version=runner.version, visibility="private",
        private_owner=("runner", runner.id), summary="Runner queued after Tool decision",
        payload={}, correlation_id=run.id, caused_by_event_id=action_event.id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.runner", action="execute", aggregate_type="runner", aggregate_id=runner.id,
        partition_key=run.session_id, dedupe_key=f"event:{queued.id}:execute",
        payload_ref={"runner_id": runner.id, "confirmation_id": confirmation.id},
    )
    response = view(confirmation, invocation)
    record.resource_type, record.resource_id, record.http_status = "tool_confirmation", confirmation.id, 200
    record.response_snapshot = response.model_dump(mode="json")
    await db.commit()
    return response
