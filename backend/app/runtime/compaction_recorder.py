"""Account for internal compaction without streaming or executing its output."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

from sqlalchemy import func, select

from app.models.common import utcnow, uuid7
from app.models.foundation import UserSession
from app.models.runtime_v1 import AgentTurn, DurableRunner, Run, WorkItem
from app.runtime.model_adapter import ModelAdapter
from app.runtime.protocol import StopReason
from app.runtime.provider_attempt_recorder import TurnRecorder
from app.services.run_reliability_service import assert_post_provider_budget, assert_turn_budget
from app.services.usage_ledger_service import record_provider_attempt_usage


class CompactionFenced(RuntimeError):
    code = "context_compaction_fenced"


def compaction_scope(identity) -> dict:
    return {key: getattr(identity, key) for key in (
        "run_id", "work_item_id", "agent_version_id", "model_version_id",
    )}


def scoped_compaction_cache(entries, identity) -> list[dict]:
    scope = compaction_scope(identity)
    return [dict(entry) for entry in (entries or [])
            if isinstance(entry, dict) and entry.get("scope") == scope]


def bind_compaction_scope(context, identity):
    if identity is None or not (context.compaction or {}).get("hard_compactions"):
        return context
    return replace(context, compaction={**context.compaction, "hard_compactions": [
        {**entry, "scope": compaction_scope(identity)}
        for entry in context.compaction["hard_compactions"]
    ]})


async def validate_compaction_owner(db, identity, *, lock=False) -> None:
    """Refresh ownership after network I/O; a stale ORM instance is not a fence."""
    if lock:
        # Follow settlement's aggregate lock order. Installation is a short
        # transaction; these locks are never retained during Provider I/O.
        session_id = await db.scalar(select(DurableRunner.session_id).where(
            DurableRunner.id == identity.runner_id,
        ))
        for model, key in ((UserSession, session_id), (Run, identity.run_id),
                           (WorkItem, identity.work_item_id), (DurableRunner, identity.runner_id)):
            await db.scalar(select(model).where(model.id == key).with_for_update())
    statement = select(
        DurableRunner.status, DurableRunner.execution_epoch, DurableRunner.lease_token,
        WorkItem.active_runner_id, WorkItem.execution_epoch, Run.status,
    ).join(WorkItem, WorkItem.id == DurableRunner.work_item_id).join(
        Run, Run.id == DurableRunner.run_id,
    ).where(DurableRunner.id == identity.runner_id)
    row = (await db.execute(statement.with_for_update() if lock else statement)).one_or_none()
    expected_token = getattr(identity, "lease_token", None)
    if (row is None or row[0] != "running" or row[1] != identity.execution_epoch
            or row[3] != identity.runner_id or row[4] != identity.execution_epoch
            or row[5] != "active" or (expected_token is not None and row[2] != expected_token)):
        raise CompactionFenced("The Runner no longer owns this context compaction")


def compaction_invoker(*, provider, db, identity, capability_id, reliability_enabled=True):
    """Use the frozen Provider, normal usage ledger and short transactions."""
    adapter = ModelAdapter(provider, max_zero_payload_retries=0)

    async def invoke(request, metadata):
        if reliability_enabled:
            await validate_compaction_owner(db, identity)
            await assert_turn_budget(db, identity.run_id)
        previous = await db.scalar(select(func.max(AgentTurn.turn_no)).where(
            AgentTurn.runner_id == identity.runner_id,
        )) or 0
        turn = AgentTurn(
            id=uuid7(), runner_id=identity.runner_id, work_item_id=identity.work_item_id,
            run_id=identity.run_id, agent_version_id=identity.agent_version_id,
            capability_snapshot_id=capability_id, turn_no=previous + 1, status="started",
            context_manifest=[{**metadata, "source": "context_compaction",
                               "scope": compaction_scope(identity)}],
            input_hash=hashlib.sha256(json.dumps(
                request.messages, ensure_ascii=False, sort_keys=True, default=str,
            ).encode()).hexdigest(), turn_items=[], usage={}, started_at=utcnow(),
        )
        db.add(turn)
        await db.commit()
        recorder = TurnRecorder(db, turn, identity, usage_ledger_enabled=reliability_enabled)
        try:
            result = await adapter.invoke(request, attempt_observer=recorder.observe)
            turn.usage = result.usage.as_dict()
            turn.stop_reason = result.stop_reason.value
            turn.turn_items = [item.as_dict() for item in result.items]
            # Paid usage is already persisted, even if cancellation/fencing or
            # invalid model output prevents this summary from being installed.
            if reliability_enabled:
                await validate_compaction_owner(db, identity)
                await assert_post_provider_budget(db, identity.run_id)
            valid = (result.stop_reason == StopReason.END_TURN and not result.tool_calls
                     and any(item.text.strip() for item in result.assistant_messages))
            turn.status = "completed" if valid else "failed"
            turn.error_code = None if valid else "context_compaction_invalid"
            turn.ended_at = utcnow()
            await db.commit()
            return result
        except BaseException as error:
            turn.status = "failed"
            turn.error_code = ("context_compaction_interrupted" if isinstance(error, asyncio.CancelledError)
                               else getattr(error, "code", "context_compaction_failed"))
            turn.ended_at = utcnow()
            for attempt in recorder.attempts.values():
                if attempt.status == "running":
                    attempt.status = "outcome_unknown" if attempt.received_payload else "timed_out"
                    attempt.error_code = turn.error_code
                    attempt.ended_at = utcnow()
                    if reliability_enabled:
                        await record_provider_attempt_usage(
                            db, attempt=attempt, turn=turn, runner_id=identity.runner_id,
                            work_item_id=identity.work_item_id, run_id=identity.run_id,
                            agent_version_id=identity.agent_version_id,
                        )
            await db.commit()
            raise

    return invoke
