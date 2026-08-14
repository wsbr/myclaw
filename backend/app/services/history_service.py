"""History policies over the single authorized Message projection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select

from app.models.common import uuid7
from app.models.foundation import ExecutionEvent, UserSession
from app.models.history_v1 import HistorySummary
from app.models.registry import PolicyDefinition, PolicyVersion
from app.models.runtime_v1 import DurableRunner, Message, Run
from app.persistence.reliable_writes import OutboxWriter
from app.services.visible_events import VisibleEventProjection


DEFAULT_HISTORY_PARAMETERS = {
    "token_budget": 24_000,
    "max_messages": 80,
    "summary_trigger_tokens": 12_000,
    "summary_token_budget": 2_000,
    "summary_recent_token_budget": 8_000,
}


def estimate_tokens(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def source_hash(messages: list[Message]) -> str:
    value = [{
        "id": item.id,
        "role": item.role,
        "status": item.status,
        "content_hash": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
    } for item in messages]
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _bounded_recent(messages: list[Message], *, token_budget: int, max_messages: int) -> tuple[list[Message], int]:
    selected: list[Message] = []
    used = 0
    for message in reversed(messages):
        cost = estimate_tokens(message.content)
        if selected and (used + cost > token_budget or len(selected) >= max_messages):
            break
        selected.append(message)
        used += cost
        if len(selected) >= max_messages:
            break
    selected.reverse()
    return selected, used


@dataclass(frozen=True)
class HistorySelection:
    messages: list[Message]
    summary: str | None
    manifest: dict


class VisibleRecordRepository:
    def __init__(self, db) -> None:
        self.db = db

    async def messages_for_runner(self, *, run: Run, runner: DurableRunner) -> list[Message]:
        return await VisibleEventProjection(self.db).list_direct_messages(
            user_id=run.user_id,
            session_id=run.session_id,
            mode=run.mode,
            group_membership_id=(runner.input_snapshot.get("group_membership_id")
                           or runner.principal_snapshot.get("group_membership_id")),
            group_run_id=(runner.input_snapshot.get("group_run_id")
                          or runner.principal_snapshot.get("group_run_id")),
            delegated=bool(runner.input_snapshot.get("agent_invocation_id")),
        )

    async def messages_for_summary(self, summary: HistorySummary) -> list[Message]:
        if summary.scope_type == "group_agent":
            run_mode, group_membership_id = "group", summary.group_membership_id
            group_run_id = summary.group_run_id
            if group_run_id is None:
                through_message = await self.db.get(Message, summary.through_message_id)
                group_run_id = through_message.group_run_id if through_message else None
        else:
            run_mode, group_membership_id = "chat", None
            group_run_id = None
        return await VisibleEventProjection(self.db).list_direct_messages(
            user_id=(await self.db.get(UserSession, summary.session_id)).user_id,
            session_id=summary.session_id,
            mode=run_mode,
            group_membership_id=group_membership_id,
            group_run_id=group_run_id,
        )


def _scope_values(
    run: Run, runner: DurableRunner,
) -> tuple[str, str | None, str | None, str | None]:
    if run.mode != "group":
        return "chat", None, None, None
    return (
        "group_agent",
        runner.input_snapshot.get("group_session_id") or runner.principal_snapshot.get("group_session_id"),
        runner.input_snapshot.get("group_membership_id") or runner.principal_snapshot.get("group_membership_id"),
        runner.input_snapshot.get("group_run_id") or runner.principal_snapshot.get("group_run_id"),
    )


def _scope_predicates(
    scope_type: str, group_session_id: str | None,
    group_membership_id: str | None, group_run_id: str | None,
):
    return (
        HistorySummary.scope_type == scope_type,
        HistorySummary.group_session_id.is_(None) if group_session_id is None else HistorySummary.group_session_id == group_session_id,
        HistorySummary.group_membership_id.is_(None) if group_membership_id is None else HistorySummary.group_membership_id == group_membership_id,
        HistorySummary.group_run_id.is_(None) if group_run_id is None else HistorySummary.group_run_id == group_run_id,
    )


async def select_history(
    db, *, run: Run, runner: DurableRunner, policy: PolicyDefinition | None,
    parameters: dict | None, model_id: str,
    policy_version: PolicyVersion | None = None,
    exclude_message_ids: set[str] | None = None,
    model_version_id: str | None = None,
) -> HistorySelection:
    policy_key = policy.policy_key if policy and policy.enabled else "history.recent"
    merged = {**DEFAULT_HISTORY_PARAMETERS, **dict(parameters or {})}
    token_budget = max(1, min(int(merged["token_budget"]), 100_000))
    max_messages = max(1, min(int(merged["max_messages"]), 200))
    visible = await VisibleRecordRepository(db).messages_for_runner(run=run, runner=runner)
    excluded = set(exclude_message_ids or ())
    if excluded:
        visible = [message for message in visible if message.id not in excluded]
    scope_type, group_session_id, group_membership_id, group_run_id = _scope_values(
        run, runner,
    )
    ready: HistorySummary | None = None
    prefix_count = 0

    if policy_key == "history.summary_recent" and visible:
        summaries = list(await db.scalars(select(HistorySummary).where(
            HistorySummary.session_id == run.session_id,
            *_scope_predicates(
                scope_type, group_session_id, group_membership_id, group_run_id,
            ),
            HistorySummary.status == "ready",
        ).order_by(HistorySummary.through_event_sequence.desc()).limit(5)))
        positions = {message.id: index for index, message in enumerate(visible)}
        for candidate in summaries:
            index = positions.get(candidate.through_message_id)
            if index is not None and source_hash(visible[:index + 1]) == candidate.source_hash:
                ready, prefix_count = candidate, index + 1
                break
            candidate.status = "superseded"
            candidate.version += 1

    recent_budget = (max(1, min(int(merged["summary_recent_token_budget"]), token_budget))
                     if ready else token_budget)
    selected, selected_tokens = _bounded_recent(
        visible[prefix_count:], token_budget=recent_budget, max_messages=max_messages,
    )
    trimmed_count = len(visible) - prefix_count - len(selected)

    if policy_key == "history.summary_recent" and ready is None and visible:
        total_tokens = sum(estimate_tokens(item.content) for item in visible)
        trigger = max(1, min(int(merged["summary_trigger_tokens"]), 100_000))
        if total_tokens > trigger:
            recent_for_summary, _ = _bounded_recent(
                visible,
                token_budget=max(1, min(int(merged["summary_recent_token_budget"]), token_budget)),
                max_messages=max_messages,
            )
            prefix = visible[:len(visible) - len(recent_for_summary)]
            if prefix:
                through = prefix[-1]
                event_sequence = None
                if through.caused_by_event_id:
                    event_sequence = await db.scalar(select(ExecutionEvent.sequence).where(
                        ExecutionEvent.id == through.caused_by_event_id,
                    ))
                if event_sequence is None:
                    session = await db.scalar(select(UserSession).where(
                        UserSession.id == run.session_id,
                    ).with_for_update())
                    event_sequence = max(0, session.next_event_sequence - 1)
                existing = await db.scalar(select(HistorySummary).where(
                    HistorySummary.session_id == run.session_id,
                    *_scope_predicates(
                        scope_type, group_session_id, group_membership_id,
                        group_run_id,
                    ),
                    HistorySummary.through_event_sequence == event_sequence,
                ))
                if existing is None:
                    pending = HistorySummary(
                        id=uuid7(), session_id=run.session_id, scope_type=scope_type,
                        group_session_id=group_session_id, group_membership_id=group_membership_id,
                        group_run_id=group_run_id,
                        through_message_id=through.id, through_event_sequence=event_sequence,
                        source_hash=source_hash(prefix), status="pending", model_id=model_id,
                        parameter_version=(
                            int(policy_version.version_no)
                            if policy_version else (policy.parameter_version if policy else 1)
                        ),
                    )
                    db.add(pending)
                    await db.flush()
                    await OutboxWriter(db).enqueue(
                        topic="runtime.maintenance", action="summarize_history",
                        aggregate_type="history_summary", aggregate_id=pending.id,
                        partition_key=run.session_id,
                        dedupe_key=f"history_summary:{pending.id}",
                        payload_ref={
                            "history_summary_id": pending.id,
                            "policy_definition_id": policy.id if policy else None,
                            "policy_version_id": policy_version.id if policy_version else None,
                            "model_id": model_id,
                            "model_version_id": model_version_id,
                        },
                    )

    return HistorySelection(
        messages=selected,
        summary=ready.summary if ready else None,
        manifest={
            "policy_key": policy_key,
            "policy_definition_id": policy.id if policy else None,
            "policy_version_id": policy_version.id if policy_version else None,
            "policy_version_no": policy_version.version_no if policy_version else None,
            "policy_content_hash": policy_version.content_hash if policy_version else None,
            "parameters": merged,
            "projection": {
                "scope_type": scope_type,
                "group_session_id": group_session_id,
                "group_membership_id": group_membership_id,
                "group_run_id": group_run_id,
            },
            "selected_message_ids": [item.id for item in selected],
            "excluded_message_ids": sorted(excluded),
            "summary_id": ready.id if ready else None,
            "trimmed_count": trimmed_count,
            "token_estimate": selected_tokens + (estimate_tokens(ready.summary) if ready and ready.summary else 0),
        },
    )


async def summary_source_messages(db, summary: HistorySummary) -> list[Message]:
    visible = await VisibleRecordRepository(db).messages_for_summary(summary)
    for index, message in enumerate(visible):
        if message.id == summary.through_message_id:
            return visible[:index + 1]
    return []
