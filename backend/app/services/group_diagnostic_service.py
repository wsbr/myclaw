"""Durable, user-safe diagnostic facts for Group Runtime decisions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import utcnow, uuid7
from app.models.group_v1 import GroupDiagnostic, GroupRun, GroupRunMember
from app.models.runtime_v1 import AgentInvocation, DurableRunner, Run, WorkItem
from app.persistence.reliable_writes import ExecutionEventWriter


GUARD_SUMMARIES = {
    "agent_invocation_self_cycle": "Delegation blocked because an Agent cannot invoke itself",
    "group_invocation_cycle": "Delegation blocked because it would re-enter the causal Agent chain",
    "group_invocation_max_depth": "Delegation blocked at the maximum Group depth",
    "group_duplicate_delegation": "Repeated delegation blocked within the same work item",
    "group_fan_out_exhausted": "Delegation blocked at the per-work-item Group fan-out limit",
    "group_member_unavailable": "Delegation blocked because the Group member is unavailable",
    "membership_policy_fixed_for_run": "Delegation blocked outside the fixed GroupRun membership",
}

# These rejections mean the Runtime contained an unsafe/redundant delegation
# attempt and the Group remained able to finish useful work. They are durable
# diagnostics, but are not business execution failures by themselves.
CONTAINED_GROUP_GUARD_REJECTIONS = frozenset({
    "agent_invocation_self_cycle",
    "group_invocation_cycle",
    "group_invocation_max_depth",
    "group_duplicate_delegation",
    "group_fan_out_exhausted",
})


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode()).hexdigest()


def group_diagnostic_view(item: GroupDiagnostic) -> dict[str, Any]:
    return {
        "id": item.id,
        "session_id": item.session_id,
        "run_id": item.run_id,
        "group_session_id": item.group_session_id,
        "group_run_id": item.group_run_id,
        "diagnostic_code": item.diagnostic_code,
        "category": item.category,
        "severity": item.severity,
        "disposition": item.disposition,
        "source_entity_type": item.source_entity_type,
        "source_entity_id": item.source_entity_id,
        "source_event_id": item.source_event_id,
        "caller_agent_id": item.caller_agent_id,
        "target_agent_id": item.target_agent_id,
        "causal_fingerprint": item.causal_fingerprint,
        "causal_chain": list(item.causal_chain or []),
        "limit_snapshot": dict(item.limit_snapshot or {}),
        "safe_details": dict(item.safe_details or {}),
        "summary": item.summary,
        "created_at": item.created_at.isoformat(),
    }


async def record_group_diagnostic(
    db: AsyncSession,
    *,
    run: Run,
    group_run: GroupRun,
    diagnostic_code: str,
    category: str,
    severity: str,
    disposition: str,
    source_entity_type: str,
    source_entity_id: str,
    dedupe_material: dict[str, Any],
    summary: str,
    caller_agent_id: str | None = None,
    target_agent_id: str | None = None,
    source_event_id: str | None = None,
    causal_chain: list[str] | None = None,
    limit_snapshot: dict[str, Any] | None = None,
    safe_details: dict[str, Any] | None = None,
) -> GroupDiagnostic:
    """Append one idempotent diagnostic and its user-visible event projection."""

    causal_fingerprint = _fingerprint({
        "run_id": run.id,
        "group_run_id": group_run.id,
        "diagnostic_code": diagnostic_code,
        **dedupe_material,
    })
    dedupe_key = f"group-diagnostic:{causal_fingerprint}"
    # Serialize the select/insert idempotency boundary on the owning aggregate.
    # This also covers rejections that cannot create an AgentInvocation because
    # the requested target does not satisfy its foreign key.
    await db.scalar(select(GroupRun.id).where(
        GroupRun.id == group_run.id,
    ).with_for_update())
    existing = await db.scalar(select(GroupDiagnostic).where(
        GroupDiagnostic.run_id == run.id,
        GroupDiagnostic.dedupe_key == dedupe_key,
    ))
    if existing is not None:
        return existing
    diagnostic = GroupDiagnostic(
        id=uuid7(),
        session_id=run.session_id,
        run_id=run.id,
        group_session_id=group_run.group_session_id,
        group_run_id=group_run.id,
        diagnostic_code=diagnostic_code,
        category=category,
        severity=severity,
        disposition=disposition,
        source_entity_type=source_entity_type,
        source_entity_id=source_entity_id,
        source_event_id=source_event_id,
        caller_agent_id=caller_agent_id,
        target_agent_id=target_agent_id,
        dedupe_key=dedupe_key,
        causal_fingerprint=causal_fingerprint,
        causal_chain=list(causal_chain or []),
        limit_snapshot=dict(limit_snapshot or {}),
        safe_details=dict(safe_details or {}),
        summary=summary,
        created_at=utcnow(),
    )
    db.add(diagnostic)
    await db.flush()
    await ExecutionEventWriter(db).append(
        session_id=run.session_id,
        run_id=run.id,
        group_session_id=group_run.group_session_id,
        group_run_id=group_run.id,
        event_type="group.diagnostic_recorded",
        event_dedupe_key=f"group-diagnostic:{diagnostic.id}",
        actor_type="service",
        actor_id="group-runtime-guard",
        entity_type="group_diagnostic",
        entity_id=diagnostic.id,
        visibility="user",
        summary=diagnostic.summary,
        payload={
            "diagnostic_id": diagnostic.id,
            "diagnostic_code": diagnostic.diagnostic_code,
            "category": diagnostic.category,
            "severity": diagnostic.severity,
            "disposition": diagnostic.disposition,
            "caller_agent_id": diagnostic.caller_agent_id,
            "target_agent_id": diagnostic.target_agent_id,
            "source_entity_type": diagnostic.source_entity_type,
            "source_entity_id": diagnostic.source_entity_id,
        },
        correlation_id=run.id,
        caused_by_event_id=source_event_id,
    )
    return diagnostic


async def record_group_invocation_guard(
    db: AsyncSession,
    *,
    run: Run,
    caller_runner: DurableRunner,
    caller_work: WorkItem,
    arguments: dict[str, Any],
    operation_key: str,
    reason_code: str,
    reason: str,
    invocation: AgentInvocation | None = None,
    source_event_id: str | None = None,
) -> GroupDiagnostic | None:
    """Record a rejected Group delegation without retaining request text."""

    if run.mode != "group":
        return None
    group_run = await db.scalar(select(GroupRun).where(GroupRun.run_id == run.id))
    if group_run is None:
        return None
    requested_target_id = str(arguments.get("agent_id") or "").strip()
    target_member_id = await db.scalar(select(GroupRunMember.agent_id).where(
        GroupRunMember.group_run_id == group_run.id,
        GroupRunMember.agent_id == requested_target_id,
    )) if requested_target_id else None
    request_fingerprint = _fingerprint({
        "target_agent_id": requested_target_id,
        "task": str(arguments.get("task") or arguments.get("request") or "").strip(),
        "input": dict(arguments.get("input") or {}),
    })
    chain = [
        str(item) for item in list(
            caller_runner.principal_snapshot.get("delegation_chain") or []
        ) if item
    ]
    full_chain = [*chain, caller_runner.agent_id]
    if target_member_id:
        full_chain.append(target_member_id)
    max_depth = int((run.hard_budget_snapshot or {}).get("max_agent_depth") or 8)
    max_fan_out = int(
        (run.hard_budget_snapshot or {}).get("max_agent_fan_out_per_work_item") or 8
    )
    return await record_group_diagnostic(
        db,
        run=run,
        group_run=group_run,
        diagnostic_code=reason_code,
        category="delegation_guard",
        severity="error",
        disposition="blocked",
        source_entity_type="agent_invocation" if invocation else "work_item",
        source_entity_id=invocation.id if invocation else caller_work.id,
        source_event_id=source_event_id,
        caller_agent_id=caller_runner.agent_id,
        target_agent_id=target_member_id,
        dedupe_material={
            "operation_key": operation_key,
            "request_fingerprint": request_fingerprint,
            "source_entity_id": invocation.id if invocation else caller_work.id,
        },
        summary=GUARD_SUMMARIES.get(
            reason_code, "Group delegation was blocked by a Runtime guard",
        ),
        causal_chain=full_chain,
        limit_snapshot={
            "max_agent_depth": max_depth,
            "max_agent_fan_out_per_work_item": max_fan_out,
        },
        safe_details={
            "reason": reason[:500],
            "request_fingerprint": request_fingerprint,
            "requested_target_fingerprint": _fingerprint(requested_target_id),
        },
    )
