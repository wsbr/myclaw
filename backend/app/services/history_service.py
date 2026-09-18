"""History policies over the single authorized Message projection."""

from __future__ import annotations

import hashlib
import json
from collections import deque
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

# Each summary covers a complete visible prefix, but the provider sees only a
# frozen parent summary plus one bounded delta. Source validation streams the
# original records; no raw history is deleted or replaced by this cache.
SUMMARY_SOURCE_MAX_MESSAGES = 200
SUMMARY_SOURCE_MAX_BYTES = 48_000
SUMMARY_COMMAND_MAX_BYTES = 192_000
SUMMARY_COMMAND_MAX_CALLS = 8
HISTORY_PAGE_SIZE = 200
MANIFEST_SOURCE_ID_LIMIT = 100


def summary_source_projection(messages: list[Message]) -> list[dict]:
    return [{
        "source_ref": f"m{index}", "message_id": item.id,
        "role": item.role, "content": item.content,
    } for index, item in enumerate(messages, 1)]


def summary_source_is_bounded(messages: list[Message]) -> bool:
    return (
        len(messages) <= SUMMARY_SOURCE_MAX_MESSAGES
        and len(json.dumps(
            summary_source_projection(messages), ensure_ascii=False,
        ).encode("utf-8")) <= SUMMARY_SOURCE_MAX_BYTES
    )


def estimate_tokens(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


class SourceHasher:
    """Streaming equivalent of the existing canonical full-prefix digest."""

    def __init__(self):
        self.digest = hashlib.sha256(b"[")
        self.count = 0

    def add(self, item: Message) -> None:
        if self.count:
            self.digest.update(b",")
        value = {
            "id": item.id, "role": item.role, "status": item.status,
            "content_hash": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
        }
        self.digest.update(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
        self.count += 1

    def hexdigest(self) -> str:
        result = self.digest.copy()
        result.update(b"]")
        return result.hexdigest()


def source_hash(messages: list[Message]) -> str:
    digest = SourceHasher()
    for message in messages:
        digest.add(message)
    return digest.hexdigest()


def summary_text_hash(summary: str | None) -> str:
    return hashlib.sha256((summary or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HistorySelection:
    messages: list[Message]
    summary: str | None
    manifest: dict


class VisibleRecordRepository:
    def __init__(self, db) -> None:
        self.db = db

    def runner_scope(self, run: Run, runner: DurableRunner) -> dict:
        return {
            "user_id": run.user_id, "session_id": run.session_id, "mode": run.mode,
            "group_membership_id": (runner.input_snapshot.get("group_membership_id")
                                    or runner.principal_snapshot.get("group_membership_id")),
            "group_run_id": (runner.input_snapshot.get("group_run_id")
                             or runner.principal_snapshot.get("group_run_id")),
            "delegated": bool(runner.input_snapshot.get("agent_invocation_id")),
        }

    async def messages_for_runner(self, *, run: Run, runner: DurableRunner) -> list[Message]:
        return await VisibleEventProjection(self.db).list_direct_messages(**self.runner_scope(run, runner))

    async def read_for_runner(self, *, run: Run, runner: DurableRunner, message_ids: list[str]) -> list[Message]:
        """Exact raw-record lookup under the same scope, including older records."""
        return await VisibleEventProjection(self.db).list_direct_messages(
            **self.runner_scope(run, runner), message_ids=message_ids, hard_limit=200,
        )

    async def iter_visible(self, **scope):
        projection = VisibleEventProjection(self.db)
        newest = await projection.list_direct_messages(**scope, hard_limit=1)
        if not newest:
            return
        through = (newest[0].created_at, newest[0].id)
        after = None
        while True:
            page = await projection.list_direct_messages(
                **scope, hard_limit=HISTORY_PAGE_SIZE, oldest_first=True,
                after=after, through=through,
            )
            if not page:
                return
            for message in page:
                yield message
            after = (page[-1].created_at, page[-1].id)

    def iter_for_runner(self, *, run: Run, runner: DurableRunner):
        return self.iter_visible(**self.runner_scope(run, runner))

    async def summary_scope(self, summary: HistorySummary) -> dict:
        if summary.scope_type == "group_agent":
            run_mode, group_membership_id = "group", summary.group_membership_id
            group_run_id = summary.group_run_id
            if group_run_id is None:
                through_message = await self.db.get(Message, summary.through_message_id)
                group_run_id = through_message.group_run_id if through_message else None
        else:
            run_mode, group_membership_id = "chat", None
            group_run_id = None
        session = await self.db.get(UserSession, summary.session_id)
        if session is None:
            raise LookupError("summary session not found")
        return dict(user_id=session.user_id, session_id=summary.session_id,
                    mode=run_mode, group_membership_id=group_membership_id,
                    group_run_id=group_run_id)



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


class _RecentWindow:
    def __init__(self, token_budget: int, max_messages: int):
        self.budget, self.limit = token_budget, max_messages
        self.items = deque()
        self.tokens = 0

    def append(self, message: Message, ordinal: int):
        self.items.append((message, ordinal))
        self.tokens += estimate_tokens(message.content)
        removed = []
        while len(self.items) > 1 and (self.tokens > self.budget or len(self.items) > self.limit):
            old, _ = self.items.popleft()
            self.tokens -= estimate_tokens(old.content)
            removed.append(old)
        return removed


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
    recent_budget = max(1, min(int(merged["summary_recent_token_budget"]), token_budget))
    max_messages = max(1, min(int(merged["max_messages"]), 200))
    excluded = set(exclude_message_ids or ())
    scope_values = _scope_values(run, runner)
    scope = _scope_predicates(*scope_values)
    use_summary = policy_key == "history.summary_recent"
    candidates = list(await db.scalars(select(HistorySummary).where(
        HistorySummary.session_id == run.session_id, *scope,
        HistorySummary.status == "ready",
    ).order_by(HistorySummary.through_event_sequence.desc()).limit(5))) if use_summary else []
    candidates_by_message = {candidate.through_message_id: candidate for candidate in candidates}
    ready = None
    prefix_count = 0
    full_hash = SourceHasher()
    uncovered_hash = SourceHasher()
    uncovered_ids = []
    uncovered_first = uncovered_last = None
    selected = _RecentWindow(token_budget, max_messages)
    recent_for_summary = _RecentWindow(recent_budget, max_messages)
    total_tokens = 0
    # At most one bounded delta and two bounded recent windows remain in memory.
    # Full-prefix validation still costs O(n) database reads, streamed in pages.
    batch = []
    batch_full = False
    excluded_at = None
    invalid_summary = False
    repository = VisibleRecordRepository(db)

    async def visible():
        if use_summary:
            async for message in repository.iter_for_runner(run=run, runner=runner):
                yield message
        else:
            for message in await repository.messages_for_runner(run=run, runner=runner):
                yield message

    async for message in visible():
        full_hash.add(message)
        ordinal = full_hash.count
        candidate = candidates_by_message.get(message.id)
        if candidate is not None:
            if candidate.summary and candidate.source_hash == full_hash.hexdigest():
                ready, prefix_count = candidate, ordinal
                selected = _RecentWindow(recent_budget, max_messages)
                recent_for_summary = _RecentWindow(recent_budget, max_messages)
                uncovered_hash = SourceHasher()
                uncovered_ids = []
                uncovered_first = uncovered_last = None
                total_tokens = 0
                batch, batch_full, excluded_at = [], False, None
                continue
            invalid_summary = True
        if message.id in excluded:
            if excluded_at is None:
                excluded_at = ordinal
            continue
        total_tokens += estimate_tokens(message.content)
        for removed in selected.append(message, ordinal):
            uncovered_hash.add(removed)
            uncovered_first = uncovered_first or removed.id
            uncovered_last = removed.id
            if len(uncovered_ids) < MANIFEST_SOURCE_ID_LIMIT:
                uncovered_ids.append(removed.id)
        recent_for_summary.append(message, ordinal)
        if use_summary and not batch_full and excluded_at is None:
            prospective = [item[0] for item in batch] + [message]
            if summary_source_is_bounded(prospective):
                batch.append((message, full_hash.hexdigest(), ordinal))
            elif not batch and len(message.content.encode("utf-8")) <= SUMMARY_COMMAND_MAX_BYTES:
                # One large record is summarized in bounded, traceable slices by
                # the worker; coverage advances only after all slices succeed.
                batch.append((message, full_hash.hexdigest(), ordinal))
                batch_full = True
            else:
                batch_full = True

    if candidates and ready is None:
        invalid_summary = True
    refresh = {"status": "not_needed", "summary_id": None}
    degraded_reason = "summary_source_changed" if invalid_summary and ready is None else None
    trigger = max(1, min(int(merged["summary_trigger_tokens"]), 100_000))
    if use_summary and (uncovered_hash.count or total_tokens > trigger):
        end = recent_for_summary.items[0][1] - 1 if recent_for_summary.items else full_hash.count
        if excluded_at is not None:
            end = min(end, excluded_at - 1)
        batch = [item for item in batch if item[2] <= end]
        refresh.update({
            "source_limited": bool(batch and batch[-1][2] < end),
            "source_max_messages": SUMMARY_SOURCE_MAX_MESSAGES,
            "source_max_bytes": SUMMARY_SOURCE_MAX_BYTES,
            "command_max_bytes": SUMMARY_COMMAND_MAX_BYTES,
            "command_max_calls": SUMMARY_COMMAND_MAX_CALLS,
        })
        if batch:
            refresh.update(await _queue_summary_refresh(
                db, run=run, policy=policy, policy_version=policy_version,
                model_id=model_id, model_version_id=model_version_id,
                scope_values=scope_values, parent=ready, batch=batch,
            ))
            if refresh["status"] == "failed":
                degraded_reason = "summary_failed"
            elif refresh["status"] == "boundary_conflict":
                degraded_reason = "summary_boundary_conflict"
            elif uncovered_hash.count:
                degraded_reason = degraded_reason or "summary_pending"
        elif excluded_at is not None and end <= prefix_count:
            refresh["status"] = "excluded_input"
            degraded_reason = "summary_current_input_boundary"
        elif end > prefix_count:
            refresh["status"] = "source_limit"
            degraded_reason = "summary_source_limit"

    selected_messages = [item[0] for item in selected.items]
    return HistorySelection(
        messages=selected_messages,
        summary=ready.summary if ready else None,
        manifest={
            "policy_key": policy_key,
            "policy_definition_id": policy.id if policy else None,
            "policy_version_id": policy_version.id if policy_version else None,
            "policy_version_no": policy_version.version_no if policy_version else None,
            "policy_content_hash": policy_version.content_hash if policy_version else None,
            "parameters": merged,
            "projection": dict(zip(("scope_type", "group_session_id", "group_membership_id", "group_run_id"), scope_values)),
            "source_scan": {
                "kind": "complete_prefix" if use_summary else "recent_window",
                "message_count": full_hash.count,
                "max_messages": None if use_summary else 1000,
                "may_omit_older": not use_summary and full_hash.count >= 1000,
            },
            "selected_message_ids": [item.id for item in selected_messages],
            "excluded_message_ids": sorted(excluded),
            "summary_id": ready.id if ready else None,
            "summary_coverage": ({
                "through_message_id": ready.through_message_id,
                "through_event_sequence": ready.through_event_sequence,
                "source_hash": ready.source_hash, "message_count": prefix_count,
            } if ready else None),
            "summary_refresh": refresh,
            "uncovered_message_ids": uncovered_ids,
            "uncovered_message_count": uncovered_hash.count,
            "uncovered_ids_truncated": uncovered_hash.count > len(uncovered_ids),
            "uncovered_range": ({"first_message_id": uncovered_first, "last_message_id": uncovered_last}
                                if uncovered_hash.count else None),
            "uncovered_source_hash": uncovered_hash.hexdigest() if uncovered_hash.count else None,
            "degraded_reason": degraded_reason,
            "trimmed_count": uncovered_hash.count,
            "token_estimate": selected.tokens + (estimate_tokens(ready.summary) if ready and ready.summary else 0),
        },
    )


async def _queue_summary_refresh(
    db, *, run, policy, policy_version, model_id, model_version_id,
    scope_values, parent, batch,
) -> dict:
    scope_type, group_session_id, group_membership_id, group_run_id = scope_values
    scope = _scope_predicates(*scope_values)
    # The Session lock serializes creation where no summary row exists yet.
    session = await db.scalar(select(UserSession).where(
        UserSession.id == run.session_id,
    ).execution_options(populate_existing=True).with_for_update())
    pending = await db.scalar(select(HistorySummary).where(
        HistorySummary.session_id == run.session_id, *scope,
        HistorySummary.status == "pending",
    ).order_by(HistorySummary.through_event_sequence.desc()).limit(1))
    through, digest, covered_count = batch[-1]
    existing = pending or await db.scalar(select(HistorySummary).where(
        HistorySummary.session_id == run.session_id, *scope,
        HistorySummary.through_message_id == through.id,
        HistorySummary.source_hash == digest,
    ).limit(1))
    if existing is not None:
        return {"status": existing.status, "summary_id": existing.id,
                "through_message_id": existing.through_message_id}
    event_sequence = None
    if through.caused_by_event_id:
        event_sequence = await db.scalar(select(ExecutionEvent.sequence).where(
            ExecutionEvent.id == through.caused_by_event_id,
            ExecutionEvent.session_id == run.session_id,
        ))
    if event_sequence is None:
        event_sequence = max(0, session.next_event_sequence - 1)
    collision = await db.scalar(select(HistorySummary.id).where(
        HistorySummary.session_id == run.session_id,
        HistorySummary.scope_discriminator == (
            "chat" if scope_type == "chat" else f"group_membership:{group_membership_id}"
        ),
        HistorySummary.through_event_sequence == event_sequence,
    ))
    if collision is not None:
        return {"status": "boundary_conflict", "summary_id": None}
    pending = HistorySummary(
        id=uuid7(), session_id=run.session_id, scope_type=scope_type,
        group_session_id=group_session_id, group_membership_id=group_membership_id,
        group_run_id=group_run_id, through_message_id=through.id,
        through_event_sequence=event_sequence, source_hash=digest,
        status="pending", model_id=model_id,
        parameter_version=(int(policy_version.version_no) if policy_version
                           else (policy.parameter_version if policy else 1)),
    )
    db.add(pending)
    await db.flush()
    payload = {
        "history_summary_id": pending.id,
        "policy_definition_id": policy.id if policy else None,
        "policy_version_id": policy_version.id if policy_version else None,
        "model_id": model_id, "model_version_id": model_version_id,
        "source_contract": "rolling_prefix_v1",
        "source_message_count": len(batch), "covered_message_count": covered_count,
        "parent_summary": ({
            "id": parent.id, "through_message_id": parent.through_message_id,
            "source_hash": parent.source_hash,
            "summary_hash": summary_text_hash(parent.summary),
        } if parent else None),
    }
    await OutboxWriter(db).enqueue(
        topic="runtime.maintenance", action="summarize_history",
        aggregate_type="history_summary", aggregate_id=pending.id,
        partition_key=run.session_id, dedupe_key=f"history_summary:{pending.id}",
        tenant_id=run.tenant_id, account_id=run.account_id, payload_ref=payload,
    )
    return {"status": "pending", "summary_id": pending.id,
            "through_message_id": through.id, "source_message_count": len(batch),
            "parent_summary_id": parent.id if parent else None}


async def summary_source_messages(db, summary: HistorySummary, *, parent: HistorySummary | None = None) -> list[Message]:
    """Validate the complete authorized prefix while retaining only the delta.

    Even sources older than the normal recent-message window participate in the
    digest. Parent text is an input cache, never a substitute for source checks.
    """
    if parent is not None and (
        parent.status != "ready" or not parent.summary
        or any(getattr(parent, field) != getattr(summary, field) for field in (
            "session_id", "scope_type", "group_session_id", "group_membership_id", "group_run_id",
        ))
    ):
        return []
    repository = VisibleRecordRepository(db)
    digest = SourceHasher()
    parent_seen = parent is None
    delta = []
    source_bytes = 0
    async for message in repository.iter_visible(**await repository.summary_scope(summary)):
        digest.add(message)
        if parent is not None and message.id == parent.through_message_id:
            if digest.hexdigest() != parent.source_hash:
                return []
            parent_seen = True
            continue
        if parent_seen:
            delta.append(message)
            source_bytes += len(message.content.encode("utf-8"))
            if len(delta) > SUMMARY_SOURCE_MAX_MESSAGES or source_bytes > SUMMARY_COMMAND_MAX_BYTES:
                raise ValueError("summary delta exceeds command source bound")
        if message.id == summary.through_message_id:
            return delta if parent_seen and digest.hexdigest() == summary.source_hash else []
    return []
