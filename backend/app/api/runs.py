from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import CurrentAccount, get_current_account
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord, UserSession
from app.models.observability_v1 import ObservabilitySpan, ObservabilitySpanLink, RunnerTrace
from app.models.runtime_v1 import AgentInvocation, Clarification, DurableRunner, Run, ToolConfirmation, ToolInvocation, WorkItem
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_replacement_runner
from app.services.notification_delivery_service import create_notification_delivery_if_channel
from app.schemas.runtime_v1 import RunCancelRequest, RunnerResponse, RunResponse, WorkItemResponse
from app.services.visible_events import VisibleEventProjection, event_public_dict
from app.services.cursor_service import SortKey, paginate_scalars

router = APIRouter(tags=["runs"])
NON_TERMINAL_WORK = {"queued", "running", "waiting_user", "waiting_agent", "waiting_tool", "waiting_resource"}


def _expected_version(value: str | None) -> int:
    raw = (value or "").strip().strip('W/').strip('"')
    if not raw.isdigit():
        raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    return int(raw)


async def _idempotency(
    db: AsyncSession, current: CurrentAccount, operation: str, key: str, request: dict,
) -> tuple[IdempotencyRecord, bool]:
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    record = await db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.principal_type == "account",
        IdempotencyRecord.principal_id == current.account.id,
        IdempotencyRecord.operation == operation,
        IdempotencyRecord.idempotency_key == key,
    ))
    if record:
        if record.request_hash != digest:
            raise HTTPException(status_code=409, detail={"code": "idempotency_mismatch"})
        return record, True
    record = IdempotencyRecord(
        id=uuid7(), principal_type="account", principal_id=current.account.id,
        operation=operation, idempotency_key=key, request_hash=digest,
        expires_at=utcnow() + timedelta(days=7),
    )
    db.add(record)
    await db.flush()
    return record, False


async def _owned_run(db: AsyncSession, run_id: str, current: CurrentAccount, *, lock: bool = False) -> Run:
    query = select(Run).where(Run.id == run_id)
    if lock:
        query = query.with_for_update()
    run = await db.scalar(query)
    if run is None or run.user_id != current.account.user_id:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    return RunResponse.model_validate(await _owned_run(db, run_id, current))


@router.get("/runs/{run_id}/timeline")
@router.get("/runs/{run_id}/events")
async def get_run_timeline(
    run_id: str, after_sequence: int = 0, limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
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
    current: CurrentAccount = Depends(get_current_account),
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
    current: CurrentAccount = Depends(get_current_account),
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
    current: CurrentAccount = Depends(get_current_account),
):
    item = await db.get(WorkItem, work_item_id)
    if item is None or item.user_id != current.account.user_id:
        raise HTTPException(status_code=404, detail="Work item not found")
    return WorkItemResponse.model_validate(item)


@router.get("/runners/{runner_id}", response_model=RunnerResponse)
async def get_runner(
    runner_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
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
    current: CurrentAccount = Depends(get_current_account),
):
    """Return safe, non-authoritative diagnostics for an owned Runner."""
    runner = await db.get(DurableRunner, runner_id)
    if runner is None:
        raise HTTPException(status_code=404, detail="Runner not found")
    await _owned_run(db, runner.run_id, current)
    trace = await db.scalar(select(RunnerTrace).where(RunnerTrace.runner_id == runner.id))
    if trace is None:
        return {
            "status": "not_available", "runner_id": runner.id, "trace": None,
            "harness": _safe_harness_diagnostics(runner), "spans": [], "links": [],
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
    """Expose only non-sensitive Harness contract/capability metadata."""
    assembly = runner.assembly_snapshot or {}
    manifest = runner.prompt_manifest or {}
    capabilities = manifest.get("capabilities") or {}
    response_contract = manifest.get("response_contract") or {}
    completion_contract = manifest.get("completion_contract") or {}
    return {
        "agent_version_id": assembly.get("agent_version_id"),
        "model_id": assembly.get("model_id"),
        "model_parameter_version": assembly.get("model_parameter_version"),
        "assembled_tool_keys": list(capabilities.get("assembled_tool_keys") or []),
        "effective_tool_keys": list(capabilities.get("effective_tool_keys") or []),
        "harness_tool_keys": list(capabilities.get("harness_tool_keys") or []),
        "response_contract": response_contract.get("key"),
        "structured_output": response_contract.get("structured_output"),
        "repair_no": response_contract.get("repair_no", 0),
        "completion_contract": {
            "key": completion_contract.get("key"),
            "status": completion_contract.get("status"),
            "reason_code": completion_contract.get("reason_code"),
            "settlement_mode": completion_contract.get("settlement_mode"),
            "required_tool_keys": list(completion_contract.get("required_tool_keys") or []),
            "satisfied_tool_keys": list(completion_contract.get("satisfied_tool_keys") or []),
            "pending_tool_keys": list(completion_contract.get("pending_tool_keys") or []),
            "unavailable_tool_keys": list(completion_contract.get("unavailable_tool_keys") or []),
            "repair_no": completion_contract.get("repair_no", 0),
        },
    }


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(
    run_id: str, body: RunCancelRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    record, replay = await _idempotency(
        db, current, "cancel_run", idempotency_key, {"run_id": run_id, **body.model_dump(mode="json")},
    )
    if replay and record.response_snapshot:
        return RunResponse.model_validate(record.response_snapshot)
    run = await _owned_run(db, run_id, current, lock=True)
    if run.lifecycle in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail={"code": "run_terminal", "lifecycle": run.lifecycle})
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
            invocation.status = "cancelled"
            invocation.error_code = "run_cancelled"
            invocation.version += 1
            confirmation = await db.scalar(select(ToolConfirmation).where(
                ToolConfirmation.tool_invocation_id == invocation.id,
                ToolConfirmation.status == "pending",
            ).with_for_update())
            if confirmation:
                confirmation.status = "cancelled"
                confirmation.decided_at = now
                confirmation.version += 1
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
    run.lifecycle = "cancelled"
    run.wait_reason = None
    run.outcome = None
    run.error_code = "cancelled_by_user"
    run.result_summary = body.reason
    run.finished_at = now
    run.version += 1
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
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    record, replay = await _idempotency(db, current, "retry_work_item", idempotency_key, {"run_id": run_id, "work_item_id": work_item_id})
    if replay and record.response_snapshot:
        return RunnerResponse.model_validate(record.response_snapshot)
    run = await _owned_run(db, run_id, current, lock=True)
    item = await db.scalar(select(WorkItem).where(WorkItem.id == work_item_id, WorkItem.run_id == run.id).with_for_update())
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    if run.lifecycle not in {"running", "waiting"} or item.status != "failed" or not item.retryable:
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
