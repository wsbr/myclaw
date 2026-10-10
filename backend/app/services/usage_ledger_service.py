"""Idempotent writers and reconciled projections for the usage/cost ledger."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import utcnow, uuid7
from app.models.foundation import UserSession
from app.models.registry import ModelVersion
from app.models.runtime_v1 import (
    AgentTurn, DurableRunner, ProviderAttempt, ToolAttempt, ToolInvocation, WorkItem,
)
from app.models.usage_v1 import UsageLedgerEntry, UsagePriceSnapshot


PROVIDER_METRICS = {
    "requests": "request",
    "input_tokens": "token",
    "cached_input_tokens": "token",
    "output_tokens": "token",
    "active_milliseconds": "millisecond",
}


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode()).hexdigest()


def price_snapshot_view(item: UsagePriceSnapshot) -> dict[str, Any]:
    return {
        "id": item.id,
        "tenant_id": item.tenant_id,
        "subject_type": item.subject_type,
        "provider": item.provider,
        "subject_key": item.subject_key,
        "metric": item.metric,
        "unit": item.unit,
        "currency": item.currency,
        "price_per_unit": str(item.price_per_unit),
        "price_status": item.price_status,
        "effective_from": item.effective_from.isoformat(),
        "effective_to": item.effective_to.isoformat() if item.effective_to else None,
        "source_label": item.source_label,
        "content_hash": item.content_hash,
        "created_at": item.created_at.isoformat(),
    }


def usage_entry_view(item: UsageLedgerEntry) -> dict[str, Any]:
    return {
        "id": item.id,
        "account_id": item.account_id,
        "session_id": item.session_id,
        "run_id": item.run_id,
        "group_run_id": item.group_run_id,
        "work_item_id": item.work_item_id,
        "runner_id": item.runner_id,
        "agent_version_id": item.agent_version_id,
        "agent_turn_id": item.agent_turn_id,
        "provider_attempt_id": item.provider_attempt_id,
        "model_version_id": item.model_version_id,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "metric": item.metric,
        "unit": item.unit,
        "quantity": str(item.quantity),
        "measurement_status": item.measurement_status,
        "price_snapshot_id": item.price_snapshot_id,
        "currency": item.currency,
        "unit_price_snapshot": (
            str(item.unit_price_snapshot) if item.unit_price_snapshot is not None else None
        ),
        "cost_amount": str(item.cost_amount) if item.cost_amount is not None else None,
        "cost_status": item.cost_status,
        "adjusts_entry_id": item.adjusts_entry_id,
        "safe_metadata": dict(item.safe_metadata or {}),
        "created_at": item.created_at.isoformat(),
    }


async def create_price_snapshot(
    db: AsyncSession,
    *,
    subject_type: str,
    provider: str | None,
    subject_key: str,
    metric: str,
    unit: str,
    currency: str,
    price_per_unit: Decimal,
    price_status: str,
    effective_from: datetime,
    effective_to: datetime | None,
    source_label: str,
    actor_account_id: str,
) -> UsagePriceSnapshot:
    payload = {
        "subject_type": subject_type,
        "provider": provider,
        "subject_key": subject_key,
        "metric": metric,
        "unit": unit,
        "currency": currency,
        "price_per_unit": str(price_per_unit),
        "price_status": price_status,
        "effective_from": effective_from.isoformat(),
        "effective_to": effective_to.isoformat() if effective_to else None,
        "source_label": source_label,
    }
    content_hash = _canonical_hash(payload)
    existing = await db.scalar(select(UsagePriceSnapshot).where(
        UsagePriceSnapshot.content_hash == content_hash,
    ))
    if existing is not None:
        return existing
    overlap = await db.scalar(select(UsagePriceSnapshot.id).where(
        UsagePriceSnapshot.subject_type == subject_type,
        UsagePriceSnapshot.provider.is_(None) if provider is None else (
            UsagePriceSnapshot.provider == provider
        ),
        UsagePriceSnapshot.subject_key == subject_key,
        UsagePriceSnapshot.metric == metric,
        UsagePriceSnapshot.currency == currency,
        or_(
            UsagePriceSnapshot.effective_to.is_(None),
            UsagePriceSnapshot.effective_to > effective_from,
        ),
        True if effective_to is None else (
            UsagePriceSnapshot.effective_from < effective_to
        ),
    ).limit(1))
    if overlap is not None:
        raise ValueError("price_effective_window_overlaps")
    item = UsagePriceSnapshot(
        id=uuid7(), content_hash=content_hash,
        subject_type=subject_type, provider=provider,
        subject_key=subject_key, metric=metric, unit=unit,
        currency=currency, price_per_unit=price_per_unit,
        price_status=price_status, effective_from=effective_from,
        effective_to=effective_to, source_label=source_label,
        created_by_account_id=actor_account_id, created_at=utcnow(),
    )
    db.add(item)
    await db.flush()
    return item


async def _applicable_price(
    db: AsyncSession,
    *, subject_type: str, provider: str | None, subject_key: str,
    metric: str, unit: str,
    observed_at: datetime,
) -> UsagePriceSnapshot | None:
    return await db.scalar(select(UsagePriceSnapshot).where(
        UsagePriceSnapshot.subject_type == subject_type,
        UsagePriceSnapshot.provider.is_(None) if provider is None else (
            UsagePriceSnapshot.provider == provider
        ),
        UsagePriceSnapshot.subject_key == subject_key,
        UsagePriceSnapshot.metric == metric,
        UsagePriceSnapshot.unit == unit,
        UsagePriceSnapshot.effective_from <= observed_at,
        or_(
            UsagePriceSnapshot.effective_to.is_(None),
            UsagePriceSnapshot.effective_to > observed_at,
        ),
    ).order_by(
        UsagePriceSnapshot.effective_from.desc(), UsagePriceSnapshot.id.desc(),
    ).limit(1))


async def record_provider_attempt_usage(
    db: AsyncSession,
    *, attempt: ProviderAttempt, turn: AgentTurn,
    runner_id: str, work_item_id: str, run_id: str,
    agent_version_id: str,
) -> list[UsageLedgerEntry]:
    """Append one stable set of entries for every terminal Provider attempt."""

    if attempt.status not in {"succeeded", "failed", "timed_out", "outcome_unknown"}:
        return []
    work = await db.get(WorkItem, work_item_id)
    session = await db.get(UserSession, work.session_id if work else None)
    if work is None or session is None:
        raise LookupError("provider attempt attribution is incomplete")
    account_id = session.account_id
    model = await db.get(ModelVersion, attempt.model_version_id)
    if model is None:
        raise LookupError("provider attempt model version disappeared")
    observed_at = attempt.ended_at or utcnow()
    usage = dict(attempt.usage or {})
    # A model capability is not evidence that this response reported usage.
    # Each counter has independent presence, including an explicit zero.
    # A malformed model output can still have a valid billed usage report.
    def token_quantity(metric: str) -> tuple[Decimal, str]:
        value = usage.get(metric)
        return _decimal(value), "measured" if value is not None else "unknown"
    active_ms = Decimal(0)
    active_status = "unknown"
    if attempt.started_at is not None and attempt.ended_at is not None:
        active_ms = _decimal(max(
            0.0, (attempt.ended_at - attempt.started_at).total_seconds() * 1000,
        ))
        active_status = "measured"
    quantities = {
        "requests": (Decimal(1), "measured"),
        "input_tokens": token_quantity("input_tokens"),
        "cached_input_tokens": token_quantity("cached_input_tokens"),
        "output_tokens": token_quantity("output_tokens"),
        "active_milliseconds": (active_ms, active_status),
    }
    source_key = f"provider_attempt:{attempt.id}"
    created: list[UsageLedgerEntry] = []
    for metric, (quantity, measurement_status) in quantities.items():
        existing = await db.scalar(select(UsageLedgerEntry).where(
            UsageLedgerEntry.source_operation_key == source_key,
            UsageLedgerEntry.metric == metric,
        ))
        if existing is not None:
            created.append(existing)
            continue
        unit = PROVIDER_METRICS[metric]
        price = None
        if measurement_status != "unknown":
            price = await _applicable_price(
                db, subject_type="model", provider=model.provider,
                subject_key=model.model_name,
                metric=metric, unit=unit, observed_at=observed_at,
            )
        item = UsageLedgerEntry(
            id=uuid7(), tenant_id=session.tenant_id,
            account_id=account_id, session_id=session.id,
            run_id=run_id, group_run_id=work.group_run_id,
            work_item_id=work.id, runner_id=runner_id,
            agent_version_id=agent_version_id, agent_turn_id=turn.id,
            provider_attempt_id=attempt.id,
            model_version_id=attempt.model_version_id,
            source_type="provider_attempt", source_id=attempt.id,
            source_operation_key=source_key, metric=metric, unit=unit,
            quantity=quantity, measurement_status=measurement_status,
            price_snapshot_id=price.id if price else None,
            currency=price.currency if price else None,
            unit_price_snapshot=price.price_per_unit if price else None,
            cost_amount=(quantity * price.price_per_unit) if price else None,
            cost_status=price.price_status if price else "unknown",
            safe_metadata={
                "attempt_status": attempt.status,
                "provider": model.provider,
                "model_name": model.model_name,
                "attempt_no": attempt.attempt_no,
                "provider_request_id_present": bool(attempt.provider_request_id),
            },
            created_at=observed_at,
        )
        db.add(item)
        await db.flush()
        created.append(item)
    return created


async def record_tool_attempt_usage(
    db: AsyncSession,
    *, attempt: ToolAttempt, invocation: ToolInvocation,
) -> list[UsageLedgerEntry]:
    """Record every settled Tool/Sandbox attempt, including retries and unknowns."""

    if attempt.status not in {"succeeded", "failed", "timed_out", "cancelled", "outcome_unknown"}:
        return []
    work = await db.get(WorkItem, invocation.work_item_id)
    session = await db.get(UserSession, invocation.session_id)
    runner = await db.get(DurableRunner, invocation.runner_id)
    if work is None or session is None or runner is None:
        raise LookupError("tool attempt attribution is incomplete")
    account_id = session.account_id
    observed_at = attempt.ended_at or utcnow()
    active_ms = Decimal(0)
    active_status = "unknown"
    if attempt.started_at is not None and attempt.ended_at is not None:
        active_ms = _decimal(max(
            0.0, (attempt.ended_at - attempt.started_at).total_seconds() * 1000,
        ))
        active_status = "measured"
    metrics = [
        ("tool_units", "invocation", Decimal(1), "tool", invocation.tool_key, "measured"),
        (
            "sandbox_milliseconds", "millisecond", active_ms,
            "sandbox", invocation.execution_mode, active_status,
        ),
    ]
    source_key = f"tool_attempt:{attempt.id}"
    created: list[UsageLedgerEntry] = []
    for metric, unit, quantity, subject_type, subject_key, measurement_status in metrics:
        existing = await db.scalar(select(UsageLedgerEntry).where(
            UsageLedgerEntry.source_operation_key == source_key,
            UsageLedgerEntry.metric == metric,
        ))
        if existing is not None:
            created.append(existing)
            continue
        price = None
        if measurement_status != "unknown":
            price = await _applicable_price(
                db, subject_type=subject_type, provider=None,
                subject_key=subject_key, metric=metric, unit=unit,
                observed_at=observed_at,
            )
        item = UsageLedgerEntry(
            id=uuid7(), tenant_id=session.tenant_id,
            account_id=account_id, session_id=session.id,
            run_id=invocation.run_id, group_run_id=work.group_run_id,
            work_item_id=work.id, runner_id=runner.id,
            agent_version_id=runner.agent_version_id,
            agent_turn_id=invocation.source_turn_id,
            tool_invocation_id=invocation.id, tool_attempt_id=attempt.id,
            source_type="tool_attempt", source_id=attempt.id,
            source_operation_key=source_key, metric=metric, unit=unit,
            quantity=quantity, measurement_status=measurement_status,
            price_snapshot_id=price.id if price else None,
            currency=price.currency if price else None,
            unit_price_snapshot=price.price_per_unit if price else None,
            cost_amount=(quantity * price.price_per_unit) if price else None,
            cost_status=price.price_status if price else "unknown",
            safe_metadata={
                "attempt_status": attempt.status,
                "attempt_no": attempt.attempt_no,
                "tool_key": invocation.tool_key,
                "execution_mode": invocation.execution_mode,
            },
            created_at=observed_at,
        )
        db.add(item)
        await db.flush()
        created.append(item)
    return created


async def create_usage_adjustment(
    db: AsyncSession,
    *, original: UsageLedgerEntry, quantity_delta: Decimal,
    reason: str, operation_key: str,
) -> UsageLedgerEntry:
    """Append a correction while preserving the original measurement."""

    existing = await db.scalar(select(UsageLedgerEntry).where(
        UsageLedgerEntry.source_operation_key == operation_key,
        UsageLedgerEntry.metric == original.metric,
    ))
    if existing is not None:
        return existing
    adjustment_id = uuid7()
    has_price = (
        original.price_snapshot_id is not None
        and original.unit_price_snapshot is not None
        and original.currency is not None
        and original.cost_status != "unknown"
    )
    item = UsageLedgerEntry(
        id=adjustment_id,
        tenant_id=original.tenant_id,
        account_id=original.account_id,
        session_id=original.session_id,
        run_id=original.run_id,
        group_run_id=original.group_run_id,
        work_item_id=original.work_item_id,
        runner_id=original.runner_id,
        agent_version_id=original.agent_version_id,
        agent_turn_id=original.agent_turn_id,
        provider_attempt_id=original.provider_attempt_id,
        model_version_id=original.model_version_id,
        tool_invocation_id=original.tool_invocation_id,
        tool_attempt_id=original.tool_attempt_id,
        source_type="adjustment",
        source_id=adjustment_id,
        source_operation_key=operation_key,
        metric=original.metric,
        unit=original.unit,
        quantity=quantity_delta,
        measurement_status=original.measurement_status,
        price_snapshot_id=original.price_snapshot_id if has_price else None,
        currency=original.currency if has_price else None,
        unit_price_snapshot=original.unit_price_snapshot if has_price else None,
        cost_amount=(
            quantity_delta * original.unit_price_snapshot if has_price else None
        ),
        cost_status=original.cost_status if has_price else "unknown",
        adjusts_entry_id=original.id,
        safe_metadata={"reason": reason.strip()[:500]},
        created_at=utcnow(),
    )
    db.add(item)
    await db.flush()
    return item


def usage_rollup(entries: list[UsageLedgerEntry], *, group_by: str) -> list[dict[str, Any]]:
    """Reconcile entry rows without treating unknown price as zero cost."""

    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "quantities": defaultdict(Decimal),
        "costs": defaultdict(Decimal),
        "unknown_cost_entries": 0,
        "entry_count": 0,
    })
    for item in entries:
        key = str(getattr(item, group_by) or "unattributed")
        bucket = grouped[key]
        bucket["entry_count"] += 1
        bucket["quantities"][f"{item.metric}:{item.unit}"] += item.quantity
        if item.cost_status == "unknown" or item.cost_amount is None:
            bucket["unknown_cost_entries"] += 1
        else:
            bucket["costs"][f"{item.currency}:{item.cost_status}"] += item.cost_amount
    return [{
        "key": key,
        "entry_count": bucket["entry_count"],
        "quantities": {
            name: str(value) for name, value in sorted(bucket["quantities"].items())
        },
        "costs": {
            name: str(value) for name, value in sorted(bucket["costs"].items())
        },
        "unknown_cost_entries": bucket["unknown_cost_entries"],
    } for key, bucket in sorted(grouped.items())]
