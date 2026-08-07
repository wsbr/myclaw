from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.sessions import _owned_session
from app.deps import CurrentAccount, SessionActor, get_session_actor
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord, UserSession
from app.models.observability_v1 import ObservabilitySpan, ObservabilitySpanLink, RunnerTrace
from app.models.runtime_v1 import (
    AgentInvocation, AgentTurn, Approval, Clarification, DurableRunner,
    ProviderAttempt, Run, ToolAttempt, ToolConfirmation, ToolInvocation,
    WorkItem, WorkingPlan,
)
from app.models.verification_v1 import VerificationResult
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_replacement_runner
from app.services.notification_delivery_service import create_notification_delivery_if_channel
from app.schemas.runtime_v1 import (
    AgentInvocationCreateRequest, RunCancelRequest, RunnerResponse,
    RunResponse, WorkItemResponse,
)
from app.services.agent_invocation_service import (
    AgentInvocationRejected, dispatch_agent_invocation,
    persist_rejected_agent_invocation,
)
from app.services.visible_events import VisibleEventProjection, event_public_dict
from app.services.cursor_service import SortKey, paginate_scalars
from app.services.verification_service import verification_result_view

router = APIRouter(tags=["runs"])
NON_TERMINAL_WORK = {
    "queued", "running", "waiting_user", "waiting_approval",
    "waiting_agent", "waiting_tool", "waiting_resource",
}


def _principal_parts(current: CurrentAccount | SessionActor) -> tuple[str, str]:
    if isinstance(current, SessionActor):
        return current.principal_type, current.principal_id
    return "account", current.account.id


def _expected_version(value: str | None) -> int:
    raw = (value or "").strip().strip('W/').strip('"')
    if not raw.isdigit():
        raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    return int(raw)


async def _idempotency(
    db: AsyncSession, current: CurrentAccount | SessionActor,
    operation: str, key: str, request: dict,
) -> tuple[IdempotencyRecord, bool]:
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    principal_type, principal_id = _principal_parts(current)
    record = await db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.principal_type == principal_type,
        IdempotencyRecord.principal_id == principal_id,
        IdempotencyRecord.operation == operation,
        IdempotencyRecord.idempotency_key == key,
    ))
    if record:
        if record.request_hash != digest:
            raise HTTPException(status_code=409, detail={"code": "idempotency_mismatch"})
        return record, True
    record = IdempotencyRecord(
        id=uuid7(), principal_type=principal_type, principal_id=principal_id,
        operation=operation, idempotency_key=key, request_hash=digest,
        expires_at=utcnow() + timedelta(days=7),
    )
    db.add(record)
    await db.flush()
    return record, False


async def _owned_run(
    db: AsyncSession, run_id: str, current: CurrentAccount | SessionActor,
    *, lock: bool = False,
) -> Run:
    query = select(Run).where(Run.id == run_id)
    if lock:
        query = query.with_for_update()
    run = await db.scalar(query)
    allowed = run is not None and current.account is not None and run.user_id == current.account.user_id
    if allowed and isinstance(current, SessionActor) and current.api_key is not None:
        session = await db.get(UserSession, run.session_id)
        allowed = bool(
            session and session.account_id == current.account.id
            and session.origin_api_key_id == current.api_key.id
            and session.entry_agent_id == current.api_key.agent_id
        )
    if not allowed:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str, db: AsyncSession = Depends(get_db),
    current: SessionActor = Depends(get_session_actor),
):
    return RunResponse.model_validate(await _owned_run(db, run_id, current))


@router.get("/sessions/{session_id}/runs", response_model=dict)
async def list_session_runs(
    session_id: str, mode: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: SessionActor = Depends(get_session_actor),
):
    await _owned_session(db, session_id, current)
    if mode is not None and mode not in {"chat", "plan", "group"}:
        raise HTTPException(status_code=422, detail={"code": "invalid_run_mode"})
    statement = select(Run).where(Run.session_id == session_id)
    if mode is not None:
        statement = statement.where(Run.mode == mode)
    rows = list(await db.scalars(statement.order_by(Run.created_at.desc(), Run.id.desc())))
    return {
        "items": [RunResponse.model_validate(row).model_dump(mode="json") for row in rows],
        "next_cursor": None,
    }


def _agent_invocation_view(row: AgentInvocation) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "caller_work_item_id": row.caller_work_item_id,
        "caller_agent_id": row.caller_agent_id,
        "target_work_item_id": row.target_work_item_id,
        "target_agent_id": row.target_agent_id,
        "target_agent_version_id": row.target_agent_version_id,
        "return_route": row.return_route,
        "request_text": row.request_text,
        "operation_key": row.operation_key,
        "request_hash": row.request_hash,
        "status": row.status,
        "result_summary": row.result_summary,
        "error_code": row.error_code,
        "result_message_id": row.result_message_id,
        "created_at": row.created_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "version": row.version,
    }


@router.get("/runs/{run_id}/agent-invocations", response_model=dict)
async def list_run_agent_invocations(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    current: SessionActor = Depends(get_session_actor),
):
    await _owned_run(db, run_id, current)
    rows = list(await db.scalars(select(AgentInvocation).where(
        AgentInvocation.run_id == run_id,
    ).order_by(AgentInvocation.created_at, AgentInvocation.id)))
    return {"items": [_agent_invocation_view(row) for row in rows], "next_cursor": None}


@router.get("/runs/{run_id}/verification-results", response_model=dict)
async def list_run_verification_results(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    current: SessionActor = Depends(get_session_actor),
):
    await _owned_run(db, run_id, current)
    rows = list(await db.scalars(select(VerificationResult).where(
        VerificationResult.run_id == run_id,
    ).order_by(
        VerificationResult.created_at.desc(), VerificationResult.id.desc(),
    )))
    return {
        "items": [await verification_result_view(db, row) for row in rows],
        "next_cursor": None,
    }


@router.post("/runs/{run_id}/agent-invocations", status_code=202)
async def create_agent_invocation_for_admin_test(
    run_id: str, body: AgentInvocationCreateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    current: SessionActor = Depends(get_session_actor),
):
    """Provide the explicitly-admin test profile from the public contract.

    Normal collaboration is model-driven through ``invoke_agent``.  This
    endpoint cannot interrupt a running model turn or choose a collaborator on
    its behalf; it can only attach a test invocation to a caller already in a
    durable ``waiting_agent`` state.
    """
    if "admin" not in current.roles:
        raise HTTPException(status_code=403, detail={"code": "admin_required"})
    request = {"run_id": run_id, **body.model_dump(mode="json")}
    record, replay = await _idempotency(
        db, current, "admin_create_agent_invocation", idempotency_key, request,
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    run = await _owned_run(db, run_id, current, lock=True)
    if body.return_route != "caller":
        raise HTTPException(
            status_code=409, detail={"code": "unsupported_return_route"},
        )
    caller_work = await db.scalar(select(WorkItem).where(
        WorkItem.id == body.caller_work_item_id,
        WorkItem.run_id == run.id,
    ).with_for_update())
    if (
        caller_work is None or caller_work.status != "waiting_agent"
        or caller_work.active_runner_id is not None
    ):
        raise HTTPException(
            status_code=409, detail={"code": "caller_not_waiting_agent"},
        )
    caller_runner = await db.scalar(select(DurableRunner).where(
        DurableRunner.work_item_id == caller_work.id,
    ).order_by(DurableRunner.execution_epoch.desc()).limit(1))
    if caller_runner is None:
        raise HTTPException(
            status_code=409, detail={"code": "resume_checkpoint_unavailable"},
        )
    arguments = {"agent_id": body.target_agent_id, "task": body.content, "input": {}}
    try:
        invocation = await dispatch_agent_invocation(
            db, operation_key=body.operation_key,
            provider_call_id=f"admin-test-{record.id}",
            caller_runner=caller_runner, caller_work=caller_work,
            run=run, arguments=arguments,
        )
    except AgentInvocationRejected as error:
        try:
            invocation = await persist_rejected_agent_invocation(
                db, caller_runner=caller_runner, caller_work=caller_work,
                run=run, arguments=arguments, operation_key=body.operation_key,
                reason=str(error),
            )
        except AgentInvocationRejected as collision:
            raise HTTPException(
                status_code=409,
                detail={"code": "agent_invocation_idempotency_collision"},
            ) from collision
        if invocation is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "agent_invocation_rejected", "message": str(error)},
            )
    response = _agent_invocation_view(invocation)
    record.resource_type, record.resource_id, record.http_status = (
        "agent_invocation", invocation.id, 202,
    )
    record.response_snapshot = response
    await db.commit()
    return response


@router.get("/runs/{run_id}/timeline")
@router.get("/runs/{run_id}/events")
async def get_run_timeline(
    run_id: str, after_sequence: int = 0, limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current: SessionActor = Depends(get_session_actor),
):
    """Return the user-visible event projection for one Run.

    The scan cursor advances across invisible and unrelated events so clients can
    resume without repeatedly examining the same Session history.
    """
    run = await _owned_run(db, run_id, current)
    limit = max(1, min(limit, 100))
    cursor = max(0, after_sequence)
    items = []
    high_watermark = cursor
    has_more = False
    while len(items) < limit:
        page = await VisibleEventProjection(db).scan_session_user(
            user_id=current.account.user_id,
            session_id=run.session_id,
            after_sequence=cursor,
            scan_limit=500,
            limit=100,
        )
        high_watermark = page.session_high_watermark
        for event in page.items:
            if event.run_id == run.id:
                items.append(event_public_dict(event))
                if len(items) >= limit:
                    break
        previous_cursor = cursor
        cursor = page.scanned_through_sequence
        has_more = cursor < high_watermark
        if not has_more or cursor <= previous_cursor or len(items) >= limit:
            break
    return {
        "items": items,
        "scanned_through_sequence": cursor,
        "session_high_watermark": high_watermark,
        "has_more": has_more,
    }


@router.get("/runs/{run_id}/work-items", response_model=dict)
async def list_run_work_items(
    run_id: str, limit: int = 100, cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: SessionActor = Depends(get_session_actor),
):
    await _owned_run(db, run_id, current)
    rows, next_cursor = await paginate_scalars(
        db, select(WorkItem).where(WorkItem.run_id == run_id),
        sort=(SortKey(WorkItem.created_at, descending=False), SortKey(WorkItem.id, descending=False)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource=f"run:{run_id}:work-items", filters={},
    )
    return {"items": [WorkItemResponse.model_validate(row).model_dump(mode="json") for row in rows],
            "next_cursor": next_cursor}


@router.get("/runs/{run_id}/runners", response_model=dict)
async def list_run_runners(
    run_id: str, limit: int = 100, cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: SessionActor = Depends(get_session_actor),
):
    """List Runner identities and lifecycle metadata for an owned Run.

    Runner events remain private; this endpoint is the user-safe discovery path
    used by the diagnostics UI. RunnerResponse deliberately excludes leases,
    snapshots, prompts, raw inputs, and error details.
    """
    await _owned_run(db, run_id, current)
    rows, next_cursor = await paginate_scalars(
        db, select(DurableRunner).where(DurableRunner.run_id == run_id),
        sort=(SortKey(DurableRunner.created_at, descending=False), SortKey(DurableRunner.id, descending=False)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource=f"run:{run_id}:runners", filters={},
    )
    return {
        "items": [RunnerResponse.model_validate(row).model_dump(mode="json") for row in rows],
        "next_cursor": next_cursor,
    }


@router.get("/work-items/{work_item_id}", response_model=WorkItemResponse)
async def get_work_item(
    work_item_id: str, db: AsyncSession = Depends(get_db),
    current: SessionActor = Depends(get_session_actor),
):
    item = await db.get(WorkItem, work_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    await _owned_run(db, item.run_id, current)
    return WorkItemResponse.model_validate(item)


@router.get("/runners/{runner_id}", response_model=RunnerResponse)
async def get_runner(
    runner_id: str, db: AsyncSession = Depends(get_db),
    current: SessionActor = Depends(get_session_actor),
):
    runner = await db.get(DurableRunner, runner_id)
    if runner is None:
        raise HTTPException(status_code=404, detail="Runner not found")
    run = await _owned_run(db, runner.run_id, current)
    del run
    return RunnerResponse.model_validate(runner)


@router.get("/runners/{runner_id}/trace")
async def get_runner_trace(
    runner_id: str, db: AsyncSession = Depends(get_db),
    current: SessionActor = Depends(get_session_actor),
):
    """Return safe, non-authoritative diagnostics for an owned Runner."""
    runner = await db.get(DurableRunner, runner_id)
    if runner is None:
        raise HTTPException(status_code=404, detail="Runner not found")
    await _owned_run(db, runner.run_id, current)
    turn_ids = list(await db.scalars(select(AgentTurn.id).where(
        AgentTurn.runner_id == runner.id,
    ).order_by(AgentTurn.created_at, AgentTurn.id)))
    provider_attempt_ids = [] if not turn_ids else list(await db.scalars(select(
        ProviderAttempt.id,
    ).where(
        ProviderAttempt.agent_turn_id.in_(turn_ids),
    ).order_by(ProviderAttempt.started_at, ProviderAttempt.id)))
    invocation_ids = list(await db.scalars(select(ToolInvocation.id).where(
        ToolInvocation.runner_id == runner.id,
    ).order_by(ToolInvocation.created_at, ToolInvocation.id)))
    tool_attempt_ids = [] if not invocation_ids else list(await db.scalars(select(
        ToolAttempt.id,
    ).where(
        ToolAttempt.tool_invocation_id.in_(invocation_ids),
    ).order_by(ToolAttempt.created_at, ToolAttempt.id)))
    verification_ids = list(await db.scalars(select(VerificationResult.id).where(
        VerificationResult.run_id == runner.run_id,
    ).order_by(VerificationResult.created_at, VerificationResult.id)))
    ledger_refs = {
        "run_id": runner.run_id,
        "work_item_id": runner.work_item_id,
        "runner_id": runner.id,
        "agent_turn_ids": turn_ids,
        "provider_attempt_ids": provider_attempt_ids,
        "tool_invocation_ids": invocation_ids,
        "tool_attempt_ids": tool_attempt_ids,
        "verification_result_ids": verification_ids,
    }
    trace = await db.scalar(select(RunnerTrace).where(RunnerTrace.runner_id == runner.id))
    if trace is None:
        return {
            "status": "not_available", "runner_id": runner.id, "trace": None,
            "harness": _safe_harness_diagnostics(runner), "spans": [], "links": [],
            "ledger_refs": ledger_refs,
        }
    spans = list(await db.scalars(
        select(ObservabilitySpan)
        .where(ObservabilitySpan.runner_trace_id == trace.id)
        .order_by(ObservabilitySpan.started_at, ObservabilitySpan.id)
    ))
    span_ids = [span.id for span in spans]
    links = [] if not span_ids else list(await db.scalars(
        select(ObservabilitySpanLink)
        .where(ObservabilitySpanLink.span_id.in_(span_ids))
        .order_by(ObservabilitySpanLink.created_at, ObservabilitySpanLink.id)
    ))
    return {
        "status": "available",
        "runner_id": runner.id,
        "ledger_refs": ledger_refs,
        "harness": _safe_harness_diagnostics(runner),
        "trace": {
            "trace_id": trace.trace_id,
            "status": trace.status,
            "sampled": trace.sampled,
            "error_code": trace.error_code,
            "started_at": trace.started_at.isoformat(),
            "ended_at": trace.ended_at.isoformat() if trace.ended_at else None,
        },
        "spans": [{
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "name": span.name,
            "span_kind": span.span_kind,
            "status": span.status,
            "started_at": span.started_at.isoformat(),
            "ended_at": span.ended_at.isoformat(),
            "duration_ms": span.duration_ms,
            "attributes": span.attributes,
            "input_tokens": span.input_tokens,
            "output_tokens": span.output_tokens,
            "error_code": span.error_code,
        } for span in spans],
        "links": [{
            "span_id": next((span.span_id for span in spans if span.id == link.span_id), None),
            "linked_trace_id": link.linked_trace_id,
            "linked_span_id": link.linked_span_id,
            "link_type": link.link_type,
            "entity_type": link.entity_type,
            "entity_id": link.entity_id,
        } for link in links],
    }


def _safe_harness_diagnostics(runner: DurableRunner) -> dict:
    """Expose only non-sensitive native capability metadata."""
    assembly = runner.assembly_snapshot or {}
    manifest = runner.prompt_manifest or {}
    capabilities = manifest.get("capabilities") or {}
    return {
        "agent_version_id": assembly.get("agent_version_id"),
        "model_id": assembly.get("model_id"),
        "model_parameter_version": assembly.get("model_parameter_version"),
        "assembled_tool_keys": list(capabilities.get("assembled_tool_keys") or []),
        "effective_tool_keys": list(capabilities.get("effective_tool_keys") or []),
        "harness_tool_keys": list(capabilities.get("harness_tool_keys") or []),
    }


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(
    run_id: str, body: RunCancelRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: SessionActor = Depends(get_session_actor),
):
    record, replay = await _idempotency(
        db, current, "cancel_run", idempotency_key, {"run_id": run_id, **body.model_dump(mode="json")},
    )
    if replay and record.response_snapshot:
        return RunResponse.model_validate(record.response_snapshot)
    run = await _owned_run(db, run_id, current, lock=True)
    if run.status in {"completed", "cancelled", "failed"}:
        raise HTTPException(status_code=409, detail={"code": "run_terminal", "status": run.status})
    expected = _expected_version(if_match)
    if run.version != expected:
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": run.version})
    await db.scalar(select(UserSession).where(UserSession.id == run.session_id).with_for_update())
    now = utcnow()
    run.cancellation_requested_at = now
    work_items = list(await db.scalars(select(WorkItem).where(WorkItem.run_id == run.id).with_for_update()))
    for work in work_items:
        if work.status not in NON_TERMINAL_WORK:
            continue
        runners = list(await db.scalars(select(DurableRunner).where(
            DurableRunner.work_item_id == work.id,
            DurableRunner.status.in_(("queued", "running")),
        ).with_for_update()))
        for runner in runners:
            old_version = runner.version
            runner.status = "cancelled"
            runner.lease_expires_at = None
            runner.version += 1
            await ExecutionEventWriter(db).append(
                session_id=run.session_id, run_id=run.id, event_type="runner.cancelled",
                event_dedupe_key=f"transition:runner:{runner.id}:{old_version}:{runner.version}:cancel",
                actor_type="user", actor_id=run.user_id, entity_type="runner", entity_id=runner.id,
                entity_version=runner.version, visibility="private", private_owner=("runner", runner.id),
                summary="Runner cancelled", payload={"reason": body.reason}, correlation_id=run.id,
            )
        invocations = list(await db.scalars(select(ToolInvocation).where(
            ToolInvocation.work_item_id == work.id,
            ToolInvocation.status.in_(("created", "waiting_confirmation", "waiting_resource", "queued", "running", "cancel_requested")),
        ).with_for_update()))
        for invocation in invocations:
            attempts = list(await db.scalars(select(ToolAttempt).where(
                ToolAttempt.tool_invocation_id == invocation.id,
                ToolAttempt.status == "running",
            ).with_for_update()))
            uncertain = any(attempt.side_effect_started for attempt in attempts)
            invocation.status = "outcome_unknown" if uncertain else "cancelled"
            invocation.error_code = (
                "run_cancelled_outcome_unknown" if uncertain else "run_cancelled"
            )
            invocation.version += 1
            for attempt in attempts:
                attempt.status = (
                    "outcome_unknown" if attempt.side_effect_started else "cancelled"
                )
                attempt.error_code = (
                    "run_cancelled_outcome_unknown"
                    if attempt.side_effect_started else "run_cancelled"
                )
                attempt.error_detail = "Run was cancelled while the ToolAttempt was in flight"
                attempt.outcome_confirmed = not attempt.side_effect_started
                attempt.lease_expires_at = None
                attempt.ended_at = now
            confirmation = await db.scalar(select(ToolConfirmation).where(
                ToolConfirmation.tool_invocation_id == invocation.id,
                ToolConfirmation.status == "pending",
            ).with_for_update())
            if confirmation:
                confirmation.status = "cancelled"
                confirmation.decided_at = now
                confirmation.version += 1
            approval = await db.scalar(select(Approval).where(
                Approval.subject_type == "tool_invocation",
                Approval.subject_id == invocation.id,
                Approval.status == "pending",
            ).with_for_update())
            if approval:
                approval.status = "cancelled"
                approval.decided_at = now
                approval.version += 1
        agent_invocations = list(await db.scalars(select(AgentInvocation).where(
            (AgentInvocation.caller_work_item_id == work.id) | (AgentInvocation.target_work_item_id == work.id),
            AgentInvocation.status.in_(("created", "queued", "running")),
        ).with_for_update()))
        for agent_invocation in agent_invocations:
            agent_invocation.status = "cancelled"
            agent_invocation.error_code = "run_cancelled"
            agent_invocation.version += 1
        clarifications = list(await db.scalars(select(Clarification).where(
            Clarification.work_item_id == work.id,
            Clarification.status == "open",
        ).with_for_update()))
        for clarification in clarifications:
            clarification.status = "cancelled"
            clarification.version += 1
        work.status = "cancelled"
        work.active_runner_id = None
        work.error_code = "run_cancelled"
        work.version += 1
    old_version = run.version
    run.status = "cancelled"
    run.wait_reason = None
    run.error_code = "cancelled_by_user"
    run.terminal_reason = "cancelled_by_user"
    run.result_summary = body.reason
    run.finished_at = now
    run.completed_at = now
    run.version += 1
    plans = list(await db.scalars(select(WorkingPlan).where(
        WorkingPlan.run_id == run.id,
        WorkingPlan.status == "active",
    ).with_for_update()))
    for plan in plans:
        plan.status = "closed"
        plan.version += 1
    cancelled_event = await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id, event_type="run.cancelled",
        event_dedupe_key=f"command:cancel_run:{record.id}",
        actor_type="user", actor_id=run.user_id, entity_type="run", entity_id=run.id,
        entity_version=run.version, visibility="user", summary="Run cancelled",
        payload={"reason": body.reason, "previous_version": old_version}, correlation_id=run.id,
    )
    await create_notification_delivery_if_channel(
        db, session_id=run.session_id, triggering_event_id=cancelled_event.id,
        event_type="run_cancelled", run_id=run.id,
    )
    response = RunResponse.model_validate(run)
    record.resource_type, record.resource_id, record.http_status = "run", run.id, 200
    record.response_snapshot = response.model_dump(mode="json")
    await db.commit()
    return response


@router.post("/runs/{run_id}/retry-work-items/{work_item_id}", response_model=RunnerResponse, status_code=202)
async def retry_work_item(
    run_id: str, work_item_id: str, idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: SessionActor = Depends(get_session_actor),
):
    record, replay = await _idempotency(db, current, "retry_work_item", idempotency_key, {"run_id": run_id, "work_item_id": work_item_id})
    if replay and record.response_snapshot:
        return RunnerResponse.model_validate(record.response_snapshot)
    run = await _owned_run(db, run_id, current, lock=True)
    item = await db.scalar(select(WorkItem).where(WorkItem.id == work_item_id, WorkItem.run_id == run.id).with_for_update())
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    if run.status in {"completed", "cancelled", "failed"} or item.status != "failed" or not item.retryable:
        raise HTTPException(status_code=409, detail={"code": "work_item_not_retryable"})
    item.execution_epoch += 1
    item.attempt_no += 1
    item.status = "queued"
    item.error_code = None
    item.error_detail = None
    item.retryable = False
    item.version += 1
    source_runner = await db.scalar(select(DurableRunner).where(
        DurableRunner.work_item_id == item.id,
    ).order_by(DurableRunner.execution_epoch.desc(), DurableRunner.created_at.desc()).limit(1))
    if source_runner is None:
        raise HTTPException(status_code=409, detail={"code": "retry_source_runner_unavailable"})
    runner = DurableRunner(
        id=uuid7(), work_item_id=item.id, run_id=run.id, session_id=run.session_id,
        agent_id=source_runner.agent_id, agent_version_id=source_runner.agent_version_id,
        execution_epoch=item.execution_epoch, status="queued",
        principal_snapshot=dict(source_runner.principal_snapshot),
        assembly_snapshot=dict(source_runner.assembly_snapshot),
        prompt_manifest=dict(source_runner.prompt_manifest),
        input_snapshot={
            **dict(source_runner.input_snapshot),
            "retry_of_work_item": item.id,
            "retry_of_runner_id": source_runner.id,
        },
    )
    await persist_replacement_runner(db, item, runner)
    event = await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id, event_type="runner.queued",
        event_dedupe_key=f"command:retry_work_item:{record.id}", actor_type="user",
        actor_id=run.user_id, entity_type="runner", entity_id=runner.id,
        entity_version=runner.version, visibility="private", private_owner=("runner", runner.id),
        summary="Retry Runner queued", payload={"attempt_no": item.attempt_no}, correlation_id=run.id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.runner", action="execute", aggregate_type="runner", aggregate_id=runner.id,
        partition_key=run.session_id, dedupe_key=f"event:{event.id}:execute", payload_ref={"runner_id": runner.id},
    )
    response = RunnerResponse.model_validate(runner)
    record.resource_type, record.resource_id, record.http_status = "runner", runner.id, 202
    record.response_snapshot = response.model_dump(mode="json")
    await db.commit()
    return response
