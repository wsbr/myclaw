"""The single Agent loop used by every phase-one execution entry path."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tool_registry.base import RuntimeContext
from app.models.common import utcnow, uuid7
from app.models.runtime_v1 import (
    AgentTurn, CapabilitySnapshot, ProviderAttempt, RunnerCheckpoint,
)
from app.runtime.action_dispatcher import ActionDispatcher, DispatchResult
from app.runtime.context_assembler import ContextPackage
from app.runtime.model_adapter import ModelAdapter
from app.runtime.protocol import (
    AssistantMessageItem,
    NormalizedTurnResult,
    ProviderAttemptEvent,
    ProviderError,
    ProviderRequest,
    StopReason,
)


class AgentLoopError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoopIdentity:
    runner_id: str
    work_item_id: str
    run_id: str
    agent_version_id: str
    model_version_id: str
    provider: str
    execution_epoch: int = 1


@dataclass(frozen=True)
class AgentLoopResult:
    status: str
    final_message: str | None
    turns: tuple[NormalizedTurnResult, ...]
    dispatches: tuple[DispatchResult, ...]
    messages: tuple[dict[str, Any], ...]


class _TurnRecorder:
    def __init__(
        self, db: AsyncSession, turn: AgentTurn, identity: LoopIdentity,
    ) -> None:
        self.db = db
        self.turn = turn
        self.identity = identity
        self.attempts: dict[int, ProviderAttempt] = {}

    async def observe(self, event: ProviderAttemptEvent) -> None:
        if event.phase == "started":
            attempt = ProviderAttempt(
                id=uuid7(), agent_turn_id=self.turn.id,
                model_version_id=self.identity.model_version_id,
                attempt_no=event.attempt_no, provider=self.identity.provider,
                status="running", request_hash=event.request_hash,
                received_payload=False, tool_call_observed=False,
                side_effect_committed=False, usage={}, started_at=utcnow(),
            )
            self.attempts[event.attempt_no] = attempt
            self.db.add(attempt)
            await self.db.flush()
            return
        attempt = self.attempts[event.attempt_no]
        attempt.received_payload = event.received_payload
        attempt.ended_at = utcnow()
        if event.phase == "succeeded" and event.result is not None:
            attempt.status = "succeeded"
            attempt.tool_call_observed = bool(event.result.tool_calls)
            attempt.stop_reason = event.result.stop_reason.value
            attempt.usage = event.result.usage.as_dict()
            attempt.provider_request_id = event.result.provider_request_id
            attempt.provider_state_ref = event.result.provider_state_ref
        else:
            error = event.error
            attempt.status = "failed"
            attempt.error_code = getattr(error, "code", type(error).__name__)
            attempt.error_detail = str(error)
        await self.db.flush()


class AgentLoopCoordinator:
    def __init__(
        self,
        adapter: ModelAdapter,
        dispatcher: ActionDispatcher,
        *,
        max_turns: int = 24,
        max_schema_repairs: int = 1,
    ) -> None:
        self.adapter = adapter
        self.dispatcher = dispatcher
        self.max_turns = max_turns
        self.max_schema_repairs = max_schema_repairs

    async def run(
        self,
        *,
        context: ContextPackage,
        tool_definitions: list[dict[str, Any]],
        capability_snapshot: CapabilitySnapshot | dict[str, Any],
        runtime_context: RuntimeContext,
        model_parameters: dict[str, Any],
        db: AsyncSession | None = None,
        identity: LoopIdentity | None = None,
    ) -> AgentLoopResult:
        messages = [dict(item) for item in context.messages]
        continuation_start = len(messages)
        turns: list[NormalizedTurnResult] = []
        dispatches: list[DispatchResult] = []
        schema_repairs = 0
        for turn_index in range(1, self.max_turns + 1):
            persisted_turn = None
            recorder = None
            if db is not None:
                if identity is None:
                    raise ValueError("identity is required when persisting an Agent loop")
                previous = await db.scalar(select(func.max(AgentTurn.turn_no)).where(
                    AgentTurn.runner_id == identity.runner_id
                )) or 0
                persisted_turn = AgentTurn(
                    id=uuid7(), runner_id=identity.runner_id,
                    work_item_id=identity.work_item_id, run_id=identity.run_id,
                    agent_version_id=identity.agent_version_id,
                    capability_snapshot_id=(
                        capability_snapshot.id
                        if isinstance(capability_snapshot, CapabilitySnapshot)
                        else capability_snapshot["id"]
                    ),
                    turn_no=int(previous) + 1, status="started",
                    context_manifest=list(context.manifest),
                    input_hash=hashlib.sha256(json.dumps(
                        messages, ensure_ascii=False, sort_keys=True, default=str
                    ).encode()).hexdigest(),
                    turn_items=[], usage={}, started_at=utcnow(),
                )
                db.add(persisted_turn)
                await db.flush()
                recorder = _TurnRecorder(db, persisted_turn, identity)
            try:
                result = await self.adapter.invoke(
                    ProviderRequest(
                        messages=tuple(messages), tools=tuple(tool_definitions),
                        parameters=model_parameters,
                    ),
                    attempt_observer=recorder.observe if recorder else None,
                )
            except ProviderError as error:
                if persisted_turn is not None:
                    persisted_turn.status = "failed"
                    persisted_turn.error_code = error.code
                    persisted_turn.ended_at = utcnow()
                    await db.flush()
                raise AgentLoopError(f"Provider call failed: {error.code}") from error
            turns.append(result)
            if persisted_turn is not None:
                persisted_turn.turn_items = [item.as_dict() for item in result.items]
                persisted_turn.stop_reason = result.stop_reason.value
                persisted_turn.usage = result.usage.as_dict()
                persisted_turn.provider_state_ref = result.provider_state_ref

            assistant_text = "".join(
                item.text for item in result.assistant_messages
            )
            tool_calls = list(result.tool_calls)
            if tool_calls:
                messages.append({
                    "role": "assistant", "content": assistant_text,
                    "turn_items": [item.as_dict() for item in result.items],
                    "tool_calls": [{
                        "id": call.call_id, "type": "tool_use", "name": call.name,
                        "input": call.arguments,
                    } for call in tool_calls],
                })
                waiting = False
                turn_dispatches: list[DispatchResult] = []
                for call in tool_calls:
                    dispatched = await self.dispatcher.dispatch(
                        call, capability_snapshot=capability_snapshot,
                        runtime_context=runtime_context, db=db,
                        run_id=identity.run_id if identity else None,
                        work_item_id=identity.work_item_id if identity else None,
                        source_turn_id=persisted_turn.id if persisted_turn else None,
                    )
                    dispatches.append(dispatched)
                    turn_dispatches.append(dispatched)
                    if not dispatched.durable_wait:
                        messages.append(dispatched.provider_message())
                    waiting = waiting or dispatched.durable_wait
                    if dispatched.schema_error:
                        schema_repairs += 1
                if persisted_turn is not None:
                    persisted_turn.status = "waiting_actions" if waiting else "completed"
                    persisted_turn.ended_at = utcnow()
                    await db.flush()
                if schema_repairs > self.max_schema_repairs:
                    raise AgentLoopError("Tool schema repair budget exhausted")
                if waiting:
                    return AgentLoopResult(
                        status="waiting_actions", final_message=None,
                        turns=tuple(turns), dispatches=tuple(dispatches),
                        messages=tuple(messages),
                    )
                if db is not None and identity is not None:
                    previous_checkpoint = await db.scalar(select(
                        func.max(RunnerCheckpoint.sequence_no)
                    ).where(RunnerCheckpoint.runner_id == identity.runner_id)) or 0
                    continuation_messages = [
                        dict(item) for item in messages[continuation_start:]
                    ]
                    checkpoint_payload = {
                        "pending_results": continuation_messages,
                        "last_agent_turn_id": persisted_turn.id if persisted_turn else None,
                    }
                    checkpoint_hash = hashlib.sha256(json.dumps(
                        checkpoint_payload, ensure_ascii=False,
                        sort_keys=True, default=str,
                    ).encode()).hexdigest()
                    db.add(RunnerCheckpoint(
                        id=uuid7(), runner_id=identity.runner_id,
                        work_item_id=identity.work_item_id,
                        sequence_no=int(previous_checkpoint) + 1,
                        checkpoint_type="tool_results_persisted",
                        state_snapshot=checkpoint_payload,
                        execution_epoch=identity.execution_epoch,
                        capability_snapshot_id=(
                            capability_snapshot.id
                            if isinstance(capability_snapshot, CapabilitySnapshot)
                            else capability_snapshot.get("id")
                        ),
                        context_manifest={"sections": list(context.manifest)},
                        compaction_ref=(context.compaction or {}).get("content_hash"),
                        result_cursor={"agent_turn_id": persisted_turn.id if persisted_turn else None},
                        pending_refs={
                            "tool_invocation_ids": [
                                item.invocation_id for item in turn_dispatches
                                if item.invocation_id
                            ],
                        },
                        content_hash=checkpoint_hash,
                        last_completed_step="tool_results_persisted",
                        safe_to_resume=True,
                    ))
                    await db.flush()
                # Tool definitions intentionally remain unchanged here. A Tool
                # used successfully is still visible on the next model turn.
                continue

            if persisted_turn is not None:
                persisted_turn.status = "completed"
                persisted_turn.ended_at = utcnow()
                await db.flush()
            if result.assistant_messages:
                messages.append({"role": "assistant", "content": assistant_text})
                return AgentLoopResult(
                    status="completed", final_message=assistant_text,
                    turns=tuple(turns), dispatches=tuple(dispatches),
                    messages=tuple(messages),
                )
            if result.stop_reason in {StopReason.MAX_TOKENS, StopReason.CONTENT_FILTER}:
                raise AgentLoopError(f"Provider stopped without a final message: {result.stop_reason}")
            raise AgentLoopError("Provider returned no Tool Call and no assistant message")
        raise AgentLoopError("Agent turn budget exhausted")
