from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.runs import _expected_version, _idempotency
from app.database import get_db
from app.deps import CurrentAccount, SessionActor, get_current_account, get_session_actor
from app.models.common import utcnow, uuid7
from app.models.foundation import UserSession
from app.models.runtime_v1 import (
    Approval, Clarification, DurableRunner, Run, SessionGrant,
    ToolConfirmation, ToolInvocation, WorkItem,
)
from app.models.group_v1 import (
    GroupRun, GroupRunMember, GroupSession,
)
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_replacement_runner
from app.schemas.runtime_v1 import (
    ApprovalDecisionRequest, ToolConfirmationDecisionRequest, ToolConfirmationResponse,
)
from app.services.cursor_service import decode_cursor, encode_cursor
from app.api.sessions import _idempotency as _session_idempotency, _owned_session
from app.services.runtime_resume_service import (
    has_pending_actions, queue_work_item_resume, tool_continuation,
)
from app.services.plan_mode_service import PlanSubjectChanged, assert_current_plan_subject
from datetime import timedelta

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


def approval_view(approval: Approval, invocation: ToolInvocation | None) -> dict:
    return {
        "id": approval.id, "type": approval.kind, "status": approval.status,
        "session_id": approval.session_id, "run_id": approval.run_id,
        "work_item_id": approval.work_item_id, "version": approval.version,
        "subject_hash": approval.subject_hash,
        "presentation": approval.presentation,
        "action": {
            "method": "POST",
            "href": f"/api/v1/approvals/{approval.id}/approve",
            "reject_href": f"/api/v1/approvals/{approval.id}/reject",
        },
        "tool_invocation_id": invocation.id if invocation else None,
        "tool_key": invocation.tool_key if invocation else None,
        "arguments": invocation.arguments if invocation else None,
        "created_at": approval.created_at.isoformat(),
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
    }


@router.get("/required-actions", response_model=dict)
@router.get("/sessions/{session_id}/required-actions", response_model=dict)
async def list_required_actions(
    session_id: str | None = None, status: str = "pending",
    action_type: str | None = Query(default=None, alias="type"),
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    if actor.account is None:
        raise HTTPException(status_code=403, detail={"code": "identity_mapping_required"})
    current = CurrentAccount(account=actor.account, roles=actor.roles)
    if session_id is not None:
        await _owned_session(db, session_id, actor)
    allowed_types = {
        "clarification", "tool", "session_grant", "membership",
        "plan_implementation", "reporter_selection",
        "tool_confirmation",
    }
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
        "group_session_id": item.group_session_id, "group_membership_id": item.group_membership_id,
    } for item in clarifications)
    approvals = list(await db.scalars(select(Approval).where(
        Approval.target_account_id == current.account.id,
        Approval.status == status,
    ).order_by(Approval.created_at, Approval.id)))
    for approval in approvals:
        invocation = (
            await db.get(ToolInvocation, approval.subject_id)
            if approval.subject_type == "tool_invocation" else None
        )
        items.append({
            "id": approval.id, "type": approval.kind,
            "status": approval.status, "session_id": approval.session_id,
            "run_id": approval.run_id, "work_item_id": approval.work_item_id,
            "version": approval.version, "subject_hash": approval.subject_hash,
            "created_at": approval.created_at.isoformat(), "expires_at": None,
            "presentation": approval.presentation,
            "action": {
                "method": "POST",
                "href": f"/api/v1/approvals/{approval.id}/approve",
                "reject_href": f"/api/v1/approvals/{approval.id}/reject",
            },
            "tool_invocation_id": invocation.id if invocation else None,
            "tool_key": invocation.tool_key if invocation else None,
            "arguments": invocation.arguments if invocation else None,
        })
    for confirmation, invocation in rows:
        item = view(confirmation, invocation).model_dump(mode="json")
        item["type"] = "tool_confirmation"
        items.append(item)
    reporter_rows = list((await db.execute(
        select(GroupRun, GroupSession, Run)
        .join(GroupSession, GroupSession.id == GroupRun.group_session_id)
        .join(Run, Run.id == GroupRun.run_id)
        .where(
            GroupSession.user_id == current.account.user_id,
            GroupSession.initiator_agent_id.is_not(None),
            Run.status == "waiting_user",
            Run.wait_reason == "waiting_user",
        )
        .order_by(GroupRun.created_at, GroupRun.id)
    )).all())
    for group_run, group, run in reporter_rows:
        reporter = await db.scalar(select(GroupRunMember).where(
            GroupRunMember.group_run_id == group_run.id,
            GroupRunMember.agent_id == group_run.reporter_agent_id,
            GroupRunMember.participation_status == "available",
        ))
        if reporter is not None:
            continue
        items.append({
            "id": group_run.id,
            "type": "reporter_selection",
            "status": "pending",
            "session_id": group.session_id,
            "run_id": run.id,
            "work_item_id": None,
            "version": group_run.version,
            "created_at": group_run.updated_at.isoformat(),
            "expires_at": None,
            "presentation": {
                "title": "Select a replacement Group Reporter",
                "summary": "The current Reporter is unavailable for natural GroupRun closure",
                "group_run_id": group_run.id,
                "reporter_agent_id": group_run.reporter_agent_id,
            },
            "action": {
                "method": "POST",
                "href": f"/api/v1/group-runs/{group_run.id}/reporter",
                "required_fields": ["reporter_agent_id"],
            },
        })
    if actor.api_key is not None:
        owned_session_ids = set(await db.scalars(select(UserSession.id).where(
            UserSession.account_id == actor.account.id,
            UserSession.origin_api_key_id == actor.api_key.id,
            UserSession.entry_agent_id == actor.api_key.agent_id,
        )))
        items = [item for item in items if item.get("session_id") in owned_session_ids]
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


@router.post("/approvals/{approval_id}/{decision}")
async def decide_approval(
    approval_id: str, decision: str, body: ApprovalDecisionRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=404, detail="Decision not found")
    if actor.account is None:
        raise HTTPException(status_code=403, detail={"code": "identity_mapping_required"})
    record, replay = await _session_idempotency(
        db, actor=actor, operation=f"{decision}_approval", key=idempotency_key,
        request={
            "approval_id": approval_id, "subject_hash": body.subject_hash,
            "reason": body.reason,
        },
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    visible_approval = await db.get(Approval, approval_id)
    if (
        visible_approval is None
        or visible_approval.target_account_id != actor.account.id
    ):
        raise HTTPException(status_code=404, detail="Approval not found")
    await _owned_session(db, visible_approval.session_id, actor)
    locked_session = await db.scalar(select(UserSession).where(
        UserSession.id == visible_approval.session_id,
    ).execution_options(populate_existing=True).with_for_update())
    approval = await db.scalar(select(Approval).where(
        Approval.id == approval_id,
    ).execution_options(populate_existing=True).with_for_update())
    if approval.version != _expected_version(if_match):
        raise HTTPException(status_code=409, detail={
            "code": "version_conflict", "current_version": approval.version,
        })
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail={
            "code": "action_already_resolved", "status": approval.status,
        })
    if body.subject_hash != approval.subject_hash:
        raise HTTPException(status_code=409, detail={"code": "approval_subject_changed"})
    if (
        approval.kind == "plan_implementation"
        and approval.subject_type == "working_plan"
    ):
        try:
            session, run, work, plan, _update = await assert_current_plan_subject(
                db, approval=approval, lock=True,
            )
        except PlanSubjectChanged:
            raise HTTPException(
                status_code=409, detail={"code": "approval_subject_changed"},
            )
        if run.status != "waiting_approval" or work.status != "waiting_approval":
            raise HTTPException(
                status_code=409, detail={"code": "approval_subject_changed"},
            )
        source_runner = await db.scalar(select(DurableRunner).where(
            DurableRunner.work_item_id == work.id,
        ).order_by(DurableRunner.execution_epoch.desc()).limit(1))
        if source_runner is None:
            raise HTTPException(
                status_code=409, detail={"code": "resume_checkpoint_unavailable"},
            )
        approval.status = "approved" if decision == "approve" else "rejected"
        approval.decision_reason = body.reason
        approval.decided_by_account_id = actor.account.id
        approval.decided_at = utcnow()
        approval.version += 1
        if decision == "approve":
            session.permission_mode = "direct"
            session.permission_revision += 1
            session.version += 1
            run.permission_mode = "direct"
            run.version += 1
        decided = await ExecutionEventWriter(db).append(
            session_id=session.id, run_id=run.id,
            event_type=f"approval.{approval.status}",
            event_dedupe_key=f"approval:{approval.id}:{approval.status}",
            actor_type=actor.principal_type, actor_id=actor.principal_id,
            entity_type="approval", entity_id=approval.id,
            entity_version=approval.version, visibility="user",
            summary=(
                f"Plan revision {plan.revision} approved for implementation"
                if decision == "approve"
                else f"Plan revision {plan.revision} rejected"
            ),
            payload={
                "approval_id": approval.id,
                "working_plan_id": plan.id,
                "plan_revision": plan.revision,
                "permission_revision": session.permission_revision,
            },
            correlation_id=run.id,
        )
        await queue_work_item_resume(
            db, work=work, run=run, source_runner=source_runner,
            pending_results=[{
                "role": "user",
                "content": (
                    f"The user approved WorkingPlan revision {plan.revision}. Continue "
                    "implementation in this same Session and WorkItem. Revise the plan "
                    "when execution evidence changes."
                    if decision == "approve"
                    else (
                        f"The user rejected WorkingPlan revision {plan.revision}. "
                        f"Reason: {body.reason or 'No reason supplied'}. Continue read-only "
                        "planning and call update_plan with a new revision before presenting again."
                    )
                ),
            }],
            reason=(
                "plan_implementation_approved"
                if decision == "approve" else "plan_implementation_rejected"
            ),
            caused_by_event_id=decided.id,
            actor_id="plan-control",
            principal_snapshot_updates=(
                {
                    "permission_revision": session.permission_revision,
                    "mode": "plan",
                }
                if decision == "approve" else None
            ),
        )
        response = approval_view(approval, None)
        response["session"] = {
            "id": session.id,
            "permission_mode": session.permission_mode,
            "permission_revision": session.permission_revision,
            "interaction_mode": session.interaction_mode,
            "interaction_revision": session.interaction_revision,
            "version": session.version,
        }
        response["run"] = {
            "id": run.id, "status": run.status,
            "permission_mode": run.permission_mode, "version": run.version,
        }
        record.resource_type, record.resource_id, record.http_status = (
            "approval", approval.id, 200,
        )
        record.response_snapshot = response
        await db.commit()
        return response
    if approval.subject_type != "tool_invocation":
        raise HTTPException(status_code=409, detail={"code": "unsupported_approval_subject"})
    invocation = await db.scalar(select(ToolInvocation).where(
        ToolInvocation.id == approval.subject_id,
    ).with_for_update())
    work = await db.scalar(select(WorkItem).where(
        WorkItem.id == approval.work_item_id,
    ).with_for_update())
    run = await db.scalar(select(Run).where(
        Run.id == approval.run_id,
    ).with_for_update())
    session = locked_session
    if invocation is None or work is None or run is None or session is None:
        raise HTTPException(status_code=409, detail={"code": "approval_subject_changed"})
    if (
        invocation.status != "waiting_confirmation"
        or invocation.version != approval.subject_version
        or invocation.authorization_snapshot.get("permission_revision") != session.permission_revision
    ):
        raise HTTPException(status_code=409, detail={"code": "approval_subject_changed"})
    source_runner = await db.get(DurableRunner, invocation.runner_id)
    if source_runner is None:
        raise HTTPException(status_code=409, detail={"code": "approval_subject_changed"})

    approval.status = "approved" if decision == "approve" else "rejected"
    approval.decision_reason = body.reason
    approval.decided_by_account_id = actor.account.id
    approval.decided_at = utcnow()
    approval.version += 1
    if decision == "approve":
        invocation.status = "queued"
        if approval.kind == "session_grant":
            scope = {
                "session_id": session.id, "account_id": actor.account.id,
                "agent_id": source_runner.agent_id, "tool_key": invocation.tool_key,
                "permission_revision": session.permission_revision,
            }
            scope_hash = hashlib.sha256(json.dumps(
                scope, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            db.add(SessionGrant(
                id=uuid7(), session_id=session.id,
                target_account_id=actor.account.id, agent_id=source_runner.agent_id,
                tool_key=invocation.tool_key, scope_hash=scope_hash, scope=scope,
                source_approval_id=approval.id, status="active",
                expires_at=utcnow() + timedelta(hours=8),
            ))
    else:
        invocation.status = "rejected"
        invocation.error_code = "tool_rejected_by_user"
        invocation.error_detail = body.reason or "User rejected Tool execution"
        invocation.result_data = {
            "status": "failed", "data": None,
            "summary": invocation.error_detail, "artifact_ids": [],
            "provider_request_id": None, "retryable": False,
            "error": {
                "code": invocation.error_code, "message": invocation.error_detail,
                "category": "authorization", "details": {},
            },
        }
    invocation.version += 1
    await db.flush()
    decided = await ExecutionEventWriter(db).append(
        session_id=session.id, run_id=run.id,
        event_type=f"approval.{approval.status}",
        event_dedupe_key=f"approval:{approval.id}:{approval.status}",
        actor_type=actor.principal_type, actor_id=actor.principal_id,
        entity_type="approval", entity_id=approval.id,
        entity_version=approval.version, visibility="user",
        summary=f"Approval {approval.status}: {invocation.tool_key}",
        payload={
            "approval_id": approval.id, "type": approval.kind,
            "tool_invocation_id": invocation.id,
        }, correlation_id=run.id,
    )
    if decision == "approve":
        queued = await ExecutionEventWriter(db).append(
            session_id=session.id, run_id=run.id,
            event_type="tool_invocation.queued",
            event_dedupe_key=f"tool_invocation:{invocation.id}:queued:approval",
            actor_type="service", actor_id="approval-control",
            entity_type="tool_invocation", entity_id=invocation.id,
            entity_version=invocation.version, visibility="user",
            summary=f"Approved Tool queued: {invocation.tool_key}",
            payload={"tool_key": invocation.tool_key}, correlation_id=run.id,
            caused_by_event_id=decided.id,
        )
        await OutboxWriter(db).enqueue(
            topic="runtime.sandbox", action="execute_tool",
            aggregate_type="tool_invocation", aggregate_id=invocation.id,
            partition_key=session.id,
            dedupe_key=f"event:{queued.id}:execute_tool",
            tenant_id=run.tenant_id, account_id=run.account_id,
            payload_ref={"tool_invocation_id": invocation.id},
        )
        another_approval = await db.scalar(select(Approval.id).where(
            Approval.work_item_id == work.id, Approval.status == "pending",
        ).limit(1))
        if another_approval is None:
            work.status = "waiting_tool"
            work.wait_reason = "waiting_tool"
            work.version += 1
            run.status = "waiting_tool"
            run.wait_reason = "waiting_tool"
            run.version += 1
    elif not await has_pending_actions(db, work_item_id=work.id):
        pending = await tool_continuation(
            db, work_item_id=work.id, source_turn_id=invocation.source_turn_id,
        )
        await queue_work_item_resume(
            db, work=work, run=run, source_runner=source_runner,
            pending_results=pending, reason="approval_rejected",
            caused_by_event_id=decided.id, actor_id="approval-control",
        )
    response = approval_view(approval, invocation)
    record.resource_type = "approval"
    record.resource_id = approval.id
    record.http_status = 200
    record.response_snapshot = response
    await db.commit()
    return response


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
    visible_confirmation = await db.get(ToolConfirmation, confirmation_id)
    if (
        visible_confirmation is None
        or visible_confirmation.user_id != current.account.user_id
    ):
        raise HTTPException(status_code=404, detail="Tool confirmation not found")
    visible_invocation = await db.get(
        ToolInvocation, visible_confirmation.tool_invocation_id,
    )
    if visible_invocation is None:
        raise HTTPException(status_code=409, detail={"code": "tool_invocation_unavailable"})
    # Event sequence allocation and Runner settlement use UserSession as the
    # stream head. Lock it before Run/WorkItem to preserve one global order.
    await db.scalar(select(UserSession).where(
        UserSession.id == visible_invocation.session_id,
    ).execution_options(populate_existing=True).with_for_update())
    confirmation = await db.scalar(select(ToolConfirmation).where(
        ToolConfirmation.id == confirmation_id,
    ).execution_options(populate_existing=True).with_for_update())
    if confirmation.version != _expected_version(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": confirmation.version})
    if confirmation.status != "pending":
        raise HTTPException(status_code=409, detail={"code": "action_already_resolved", "status": confirmation.status})
    if body.arguments_hash != confirmation.arguments_hash:
        raise HTTPException(status_code=409, detail={"code": "confirmation_arguments_changed"})
    invocation = await db.scalar(select(ToolInvocation).where(
        ToolInvocation.id == confirmation.tool_invocation_id,
    ).execution_options(populate_existing=True).with_for_update())
    if (
        invocation.arguments_hash != confirmation.arguments_hash
        or invocation.principal_hash != confirmation.principal_hash
        or invocation.tool_key != confirmation.tool_key
    ):
        raise HTTPException(status_code=409, detail={"code": "confirmation_snapshot_mismatch"})
    run = await db.scalar(select(Run).where(Run.id == invocation.run_id).with_for_update())
    work = await db.scalar(select(WorkItem).where(WorkItem.id == invocation.work_item_id).with_for_update())
    if run.status in {"completed", "cancelled", "failed"}:
        raise HTTPException(status_code=409, detail={"code": "run_terminal", "status": run.status})
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
        id=uuid7(), tenant_id=run.tenant_id,
        work_item_id=work.id, run_id=run.id, session_id=run.session_id,
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
    run.status = "active"
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
        tenant_id=run.tenant_id, account_id=run.account_id,
        payload_ref={"runner_id": runner.id, "confirmation_id": confirmation.id},
    )
    response = view(confirmation, invocation)
    record.resource_type, record.resource_id, record.http_status = "tool_confirmation", confirmation.id, 200
    record.response_snapshot = response.model_dump(mode="json")
    await db.commit()
    return response
