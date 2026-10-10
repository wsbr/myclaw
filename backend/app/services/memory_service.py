"""Memory canonicalization, candidate arbitration, recall, and safe projections."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.common import uuid7
from app.models.memory_v1 import (
    MemoryCandidateSourceMessage, MemoryHead, MemoryRecord, MemorySourceMessage,
    MemoryWriteCandidate, RunnerMemorySnapshot,
)
from app.models.runtime_v1 import Message
from app.persistence.reliable_writes import ExecutionEventWriter
from app.services.history_service import estimate_tokens


class MemoryConflict(ValueError):
    pass


def canonical_fact_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").strip().lower()
    normalized = re.sub(r"\s+", "_", normalized)
    if not normalized or len(normalized) > 255 or not re.fullmatch(r"[\w.:-]+", normalized, re.UNICODE):
        raise MemoryConflict("fact_key is invalid")
    return normalized


def normalized_content(value: str) -> tuple[str, str]:
    content = unicodedata.normalize("NFKC", value or "").strip()
    content = re.sub(r"\s+", " ", content)
    if not content or len(content) > 20000:
        raise MemoryConflict("Memory content must be 1-20000 characters")
    digest = hashlib.sha256(content.casefold().encode()).hexdigest()
    return content, digest


async def memory_sources(db, memory_id: str) -> list[dict]:
    rows = list((await db.execute(
        select(MemorySourceMessage, Message)
        .join(Message, Message.id == MemorySourceMessage.message_id)
        .where(MemorySourceMessage.memory_id == memory_id)
        .order_by(Message.created_at, Message.id)
    )).all())
    return [{
        "message_id": message.id, "session_id": message.session_id,
        "source_role": relation.source_role,
        "excerpt": message.content[:240],
    } for relation, message in rows]


async def memory_view(db, memory: MemoryRecord, head: MemoryHead | None = None) -> dict:
    head = head or await db.get(MemoryHead, memory.head_id)
    return {
        "id": memory.id, "head_id": memory.head_id, "scope": memory.scope,
        "agent_id": memory.agent_id, "fact_key": memory.fact_key,
        "content": memory.content, "status": memory.status, "version": memory.version,
        "head_version": head.version, "source_kind": memory.source_kind,
        "confidence": float(memory.confidence) if memory.confidence is not None else None,
        "sources": await memory_sources(db, memory.id),
        "created_at": memory.created_at.isoformat(), "updated_at": memory.updated_at.isoformat(),
    }


async def _locked_head(db, candidate: MemoryWriteCandidate) -> MemoryHead:
    fact_key = canonical_fact_key(candidate.fact_key)
    head = await db.scalar(select(MemoryHead).where(
        MemoryHead.tenant_id == candidate.tenant_id,
        MemoryHead.user_id == candidate.user_id, MemoryHead.scope == candidate.scope,
        MemoryHead.agent_id.is_(None) if candidate.scope == "user" else MemoryHead.agent_id == candidate.agent_id,
        MemoryHead.fact_key == fact_key,
    ).with_for_update())
    if head is not None:
        return head
    try:
        async with db.begin_nested():
            head = MemoryHead(
                id=uuid7(), tenant_id=candidate.tenant_id,
                user_id=candidate.user_id, scope=candidate.scope,
                agent_id=candidate.agent_id if candidate.scope == "user_agent" else None,
                fact_key=fact_key,
            )
            db.add(head)
            await db.flush()
    except IntegrityError:
        head = await db.scalar(select(MemoryHead).where(
            MemoryHead.tenant_id == candidate.tenant_id,
            MemoryHead.user_id == candidate.user_id, MemoryHead.scope == candidate.scope,
            MemoryHead.agent_id.is_(None) if candidate.scope == "user" else MemoryHead.agent_id == candidate.agent_id,
            MemoryHead.fact_key == fact_key,
        ).with_for_update())
    if head is None:
        raise MemoryConflict("MemoryHead creation race could not be resolved")
    return head


async def _candidate_sources(db, candidate: MemoryWriteCandidate) -> list[Message]:
    message_ids = list(await db.scalars(select(MemoryCandidateSourceMessage.message_id).where(
        MemoryCandidateSourceMessage.candidate_id == candidate.id,
    )))
    messages = list(await db.scalars(select(Message).where(
        Message.id.in_(message_ids), Message.user_id == candidate.user_id,
        Message.session_id == candidate.source_session_id,
        Message.sender_type == "user", Message.status == "committed",
    ))) if message_ids else []
    if not messages or len(messages) != len(set(message_ids)):
        raise MemoryConflict("candidate sources are missing or not user-authored")
    return messages


async def _link_sources(db, memory_id: str, messages: list[Message]) -> None:
    existing = set(await db.scalars(select(MemorySourceMessage.message_id).where(
        MemorySourceMessage.memory_id == memory_id,
    )))
    db.add_all([
        MemorySourceMessage(memory_id=memory_id, message_id=message.id, source_role="user")
        for message in messages if message.id not in existing
    ])


async def apply_memory_candidate(db, candidate_id: str) -> MemoryWriteCandidate:
    candidate = await db.scalar(select(MemoryWriteCandidate).where(
        MemoryWriteCandidate.id == candidate_id,
    ).with_for_update())
    if candidate is None:
        raise MemoryConflict("Memory candidate not found")
    if candidate.status != "pending":
        return candidate
    try:
        if candidate.scope == "user_agent" and not candidate.agent_id:
            raise MemoryConflict("user_agent candidate requires agent_id")
        messages = await _candidate_sources(db, candidate)
        content, digest = normalized_content(candidate.content)
        candidate.content = content
        candidate.normalized_hash = digest
        candidate.fact_key = canonical_fact_key(candidate.fact_key)
        head = await _locked_head(db, candidate)
        active = await db.scalar(select(MemoryRecord).where(
            MemoryRecord.head_id == head.id, MemoryRecord.status == "active",
        ).with_for_update())
        if active is not None and active.normalized_hash == digest:
            await _link_sources(db, active.id, messages)
            candidate.status = "accepted"; candidate.result_memory_id = active.id; candidate.version += 1
            result = active
        elif (head.last_user_edited_at is not None and head.last_user_edited_at >= candidate.created_at):
            candidate.status = "rejected"; candidate.rejection_code = "user_edit_wins"; candidate.version += 1
            result = None
        elif candidate.observed_head_version is not None and candidate.observed_head_version != head.version:
            candidate.status = "rejected"; candidate.rejection_code = "head_changed"; candidate.version += 1
            result = None
        elif active is not None and float(candidate.confidence or 0) < 0.8:
            candidate.status = "rejected"; candidate.rejection_code = "insufficient_confidence"; candidate.version += 1
            result = None
        else:
            previous_id = active.id if active else None
            if active is not None:
                active.status = "superseded"; active.version += 1
            head.version += 1
            await db.flush()
            result = MemoryRecord(
                id=uuid7(), tenant_id=candidate.tenant_id,
                head_id=head.id, user_id=candidate.user_id,
                agent_id=candidate.agent_id if candidate.scope == "user_agent" else None,
                scope=candidate.scope, fact_key=candidate.fact_key, content=content,
                normalized_hash=digest, status="active", source_kind="model",
                source_session_id=candidate.source_session_id,
                created_by_runner_id=candidate.runner_id,
                policy_definition_id=candidate.policy_definition_id,
                confidence=candidate.confidence, supersedes_memory_id=previous_id,
            )
            db.add(result); await db.flush(); await _link_sources(db, result.id, messages)
            candidate.status = "accepted"; candidate.result_memory_id = result.id; candidate.version += 1
        event_type = "memory.updated" if result is not None else "memory.candidate_rejected"
        await ExecutionEventWriter(db).append(
            session_id=candidate.source_session_id, event_type=event_type,
            event_dedupe_key=f"transition:memory_candidate:{candidate.id}:{candidate.status}",
            actor_type="service", actor_id="memory-worker",
            entity_type="memory_write_candidate", entity_id=candidate.id,
            entity_version=candidate.version, visibility="user",
            summary="Memory updated" if result is not None else "Memory candidate was not applied",
            payload={"candidate_id": candidate.id, "memory_id": candidate.result_memory_id,
                     "reason": candidate.rejection_code}, correlation_id=candidate.source_session_id,
        )
    except MemoryConflict as error:
        candidate.status = "rejected"; candidate.rejection_code = str(error)[:100]; candidate.version += 1
        await ExecutionEventWriter(db).append(
            session_id=candidate.source_session_id, event_type="memory.candidate_rejected",
            event_dedupe_key=f"transition:memory_candidate:{candidate.id}:rejected",
            actor_type="service", actor_id="memory-worker", entity_type="memory_write_candidate",
            entity_id=candidate.id, entity_version=candidate.version, visibility="user",
            summary="Memory candidate was not applied",
            payload={"candidate_id": candidate.id, "reason": candidate.rejection_code},
            correlation_id=candidate.source_session_id,
        )
    return candidate


async def recall_memories(
    db, *, tenant_id: str, runner_id: str, user_id: str,
    agent_id: str, policy_key: str,
    parameters: dict | None = None,
) -> list[MemoryRecord]:
    # A frozen selection preserves identity and order, not permission to keep
    # recalling a revoked fact or to cross the current principal's scope.
    statement = select(MemoryRecord).where(
        MemoryRecord.tenant_id == tenant_id,
        MemoryRecord.user_id == user_id, MemoryRecord.status == "active",
    )
    if policy_key == "memory_recall.user_agent":
        statement = statement.where(MemoryRecord.scope == "user_agent", MemoryRecord.agent_id == agent_id)
    elif policy_key == "memory_recall.user_combined":
        statement = statement.where(
            ((MemoryRecord.scope == "user") & MemoryRecord.agent_id.is_(None)) |
            ((MemoryRecord.scope == "user_agent") & (MemoryRecord.agent_id == agent_id))
        )
    else:
        return []
    snapshots = list(await db.scalars(select(RunnerMemorySnapshot).where(
        RunnerMemorySnapshot.runner_id == runner_id,
    ).order_by(RunnerMemorySnapshot.rank)))
    if snapshots:
        by_id = {row.id: row for row in await db.scalars(statement.where(
            MemoryRecord.id.in_([snapshot.memory_id for snapshot in snapshots]),
        ))}
        return [by_id[snapshot.memory_id] for snapshot in snapshots if (
            snapshot.memory_id in by_id
            and snapshot.scope == by_id[snapshot.memory_id].scope
            and snapshot.content_hash == by_id[snapshot.memory_id].normalized_hash
        )]
    merged = {"max_items": 20, "token_budget": 4_000, **dict(parameters or {})}
    max_items = max(0, min(int(merged["max_items"]), 100))
    token_budget = max(0, min(int(merged["token_budget"]), 100_000))
    if max_items == 0 or token_budget == 0:
        return []
    candidates = list(await db.scalars(statement.order_by(
        MemoryRecord.updated_at.desc(), MemoryRecord.id.desc(),
    ).limit(40)))
    chosen = []
    seen = set()
    used_tokens = 0
    for memory in sorted(candidates, key=lambda item: (item.scope != "user_agent", -item.updated_at.timestamp(), item.id)):
        if memory.fact_key in seen:
            continue
        token_cost = estimate_tokens(memory.content)
        if chosen and used_tokens + token_cost > token_budget:
            continue
        seen.add(memory.fact_key)
        chosen.append(memory)
        used_tokens += token_cost
        if len(chosen) >= max_items:
            break
    db.add_all([
        RunnerMemorySnapshot(runner_id=runner_id, memory_id=memory.id,
            content_hash=memory.normalized_hash, scope=memory.scope, rank=index,
            score=Decimal("1.0") if memory.scope == "user_agent" else Decimal("0.8"))
        for index, memory in enumerate(chosen, 1)
    ])
    return chosen
