"""The single Agent loop used by every phase-one execution entry path."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tool_registry.base import RuntimeContext
from app.models.common import utcnow, uuid7
from app.models.runtime_v1 import (
    AgentTurn, CapabilitySnapshot, DurableRunner, RunnerCheckpoint,
)
from app.runtime.action_dispatcher import ActionDispatcher, DispatchResult
from app.runtime.context_assembler import ContextPackage, request_manifest
from app.runtime.model_adapter import ModelAdapter
from app.runtime.protocol import (
    AssistantMessageItem,
    NormalizedTurnResult,
    ProviderError,
    ProviderRequest,
    StreamEvent,
    StopReason,
)
from app.runtime.context_budget import fit_context_budget
from app.runtime.hard_compaction import HardCompactionError
from app.runtime.compaction_recorder import (
    bind_compaction_scope, compaction_invoker, validate_compaction_owner,
)
from app.services.run_reliability_service import (
    BudgetExceeded, assert_post_provider_budget, assert_turn_budget,
    record_progress,
)
from app.runtime.provider_attempt_recorder import TurnRecorder as _TurnRecorder


class AgentLoopError(RuntimeError):
    def __init__(self, message: str, *, code: str = "agent_loop_error") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class LoopIdentity:
    runner_id: str
    work_item_id: str
    run_id: str
    agent_version_id: str
    model_version_id: str
    provider: str
    execution_epoch: int = 1
    lease_token: str | None = None


@dataclass(frozen=True)
class AgentLoopResult:
    status: str
    final_message: str | None
    turns: tuple[NormalizedTurnResult, ...]
    dispatches: tuple[DispatchResult, ...]
    messages: tuple[dict[str, Any], ...]
    final_stream_id: str | None = None
    continuation_messages: tuple[dict[str, Any], ...] | None = None
    loaded_tool_version_ids: tuple[str, ...] = ()
    hard_compactions: tuple[dict, ...] = ()


TurnStreamObserver = Callable[[str, StreamEvent], Awaitable[None] | None]


class AgentLoopCoordinator:
    def __init__(
        self,
        adapter: ModelAdapter,
        dispatcher: ActionDispatcher,
        *,
        max_turns: int = 24,
        max_schema_repairs: int = 1,
        reliability_enabled: bool = False,
        stream_observer: TurnStreamObserver | None = None,
    ) -> None:
        self.adapter = adapter
        self.dispatcher = dispatcher
        self.max_turns = max_turns
        self.max_schema_repairs = max_schema_repairs
        self.reliability_enabled = reliability_enabled
        self.stream_observer = stream_observer
        self._configured_transport_retries = adapter.max_zero_payload_retries

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
        messages = [dict(item) for item in (context.source_messages or context.messages)]
        source_manifest = request_manifest(messages, context.source_manifest or context.manifest)
        message_manifest = list(source_manifest)
        omitted_sections = list((context.compaction or {}).get("omitted_sections") or [])
        request_compaction = dict(context.compaction or {})
        invoke_summary = compaction_invoker(
            provider=self.adapter.provider, db=db, identity=identity,
            capability_id=(capability_snapshot.id if isinstance(capability_snapshot, CapabilitySnapshot)
                           else capability_snapshot["id"]),
            reliability_enabled=self.reliability_enabled,
        ) if db is not None and identity is not None else None
        presented_definitions = [dict(item) for item in tool_definitions]
        loaded_tool_version_ids = {
            item.get("tool_version_id") for item in presented_definitions
            if item.get("tool_version_id")
        }
        continuation_start = len(messages)
        history_start = context.history_start
        history_count = context.history_count
        turns: list[NormalizedTurnResult] = []
        dispatches: list[DispatchResult] = []
        schema_repairs = 0
        context_recovery_attempted = False
        for turn_index in range(1, self.max_turns + 1):
            persisted_turn = None
            recorder = None
            budget_state = None
            if db is not None:
                if identity is None:
                    raise ValueError("identity is required when persisting an Agent loop")
                if self.reliability_enabled:
                    try:
                        budget_state = await assert_turn_budget(db, identity.run_id)
                    except BudgetExceeded as error:
                        raise AgentLoopError(str(error), code=error.code) from error
                    provider_remaining = max(
                        0,
                        int(budget_state.limits["max_provider_attempts"])
                        - int(budget_state.usage["provider_attempts"]) - 1,
                    )
                    self.adapter.max_zero_payload_retries = min(
                        self._configured_transport_retries,
                        budget_state.remaining_transport_retries,
                        provider_remaining,
                    )
            request_count = None
            turn_web_projection = []
            turn_tool_projection = []
            turn_count_calls = 0
            source_manifest = request_manifest(messages, source_manifest)
            message_manifest = list(source_manifest)
            request_messages = messages
            history_before_count = history_count
            if db is not None and db.in_transaction():
                # Token counting may call the Provider. Release the transaction.
                await db.commit()
            if context.hard_input_limit is not None:
                provider_type = (
                    identity.provider if identity is not None
                    else type(self.adapter.provider).__name__.removesuffix("LLM").lower()
                )
                try:
                    budgeted = await fit_context_budget(
                        replace(context, messages=tuple(messages), manifest=tuple(source_manifest),
                                source_messages=None, source_manifest=None,
                                history_count=history_count, compaction=request_compaction or None),
                        provider=self.adapter.provider, provider_type=provider_type,
                        tools=presented_definitions, parameters=model_parameters,
                        invoke_summary=invoke_summary,
                    )
                except HardCompactionError as error:
                    raise AgentLoopError(str(error), code=error.code) from error
                request_count = budgeted.count
                turn_count_calls = budgeted.count_calls
                view = bind_compaction_scope(budgeted.context, identity)
                request_messages = list(view.messages)
                messages = list(view.source_messages)
                source_manifest = list(view.source_manifest)
                message_manifest = list(view.manifest)
                history_count = view.history_count
                continuation_start -= history_before_count - history_count
                request_compaction = dict(view.compaction or {})
                omitted_sections = list(request_compaction.get("omitted_sections") or [])
                turn_web_projection = budgeted.web_projection
                turn_tool_projection = budgeted.tool_projection
                if request_count.tokens > context.hard_input_limit:
                    raise AgentLoopError(
                        "Protected request content exceeds the model Context Window after budget reductions",
                        code="context_window_exceeded",
                    )
            if turn_index == 1:
                continuation_start = len(messages)
            if db is not None:
                if self.reliability_enabled and request_compaction.get("hard_compactions"):
                    try:
                        budget_state = await assert_turn_budget(db, identity.run_id)
                    except BudgetExceeded as error:
                        raise AgentLoopError(str(error), code=error.code) from error
                    self.adapter.max_zero_payload_retries = min(
                        self._configured_transport_retries,
                        budget_state.remaining_transport_retries,
                        max(0, int(budget_state.limits["max_provider_attempts"])
                            - int(budget_state.usage["provider_attempts"]) - 1),
                    )
                active_runner = await db.get(DurableRunner, identity.runner_id)
                if active_runner is not None:
                    if request_compaction.get("hard_compactions") and self.reliability_enabled:
                        await validate_compaction_owner(db, identity, lock=True)
                    active_runner.input_snapshot = {
                        **dict(active_runner.input_snapshot or {}),
                        "hard_compactions": list(request_compaction.get("hard_compactions") or []),
                    }
                    active_runner.prompt_manifest = {
                        **dict(active_runner.prompt_manifest or {}),
                        "sections": list(message_manifest),
                        "omitted_sections": list(omitted_sections),
                        "compaction": request_compaction or None,
                    }
                if request_count is not None:
                    if active_runner is not None:
                        prompt_manifest = dict(active_runner.prompt_manifest or {})
                        prompt_manifest.update({
                            "count_method": request_count.method,
                            "count_degraded": request_count.degraded,
                            "soft_input_target": context.soft_input_target,
                            "hard_input_limit": context.hard_input_limit,
                            "pre_call_count": request_count.tokens,
                            "count_calls": turn_count_calls,
                            "compaction_reason": (
                                request_compaction.get("reason")
                                or prompt_manifest.get("compaction_reason")
                            ),
                            **({
                                "web_projection_latest": turn_web_projection,
                                "tool_projection_latest": turn_tool_projection,
                            } if turn_web_projection or turn_tool_projection else {}),
                        })
                        active_runner.prompt_manifest = prompt_manifest
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
                    presented_schema_bundle_hash=hashlib.sha256(json.dumps([
                        {
                            "name": item["name"],
                            "input_schema": item.get("input_schema") or {},
                            "registration_hash": item.get("registration_hash"),
                        }
                        for item in presented_definitions
                    ], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                    context_manifest=[
                        *list(message_manifest),
                        *([{"source": "context_omissions", "sections": list(omitted_sections)}]
                          if omitted_sections else []),
                        *([{
                            "source": "request_token_count",
                            "count_method": request_count.method,
                            "pre_call_count": request_count.tokens,
                            "count_calls": turn_count_calls,
                            "soft_input_target": context.soft_input_target,
                            "hard_input_limit": context.hard_input_limit,
                            "degraded": request_count.degraded,
                            "soft_target_unmet": bool(
                                context.soft_input_target is not None
                                and request_count.tokens > context.soft_input_target
                            ),
                        }] if request_count is not None else []),
                    ],
                    input_hash=hashlib.sha256(json.dumps(
                        request_messages, ensure_ascii=False, sort_keys=True, default=str
                    ).encode()).hexdigest(),
                    turn_items=[], usage={}, started_at=utcnow(),
                )
                db.add(persisted_turn)
                # The AgentTurn must exist before ProviderAttempt callbacks,
                # but no transaction/connection may span the model call.
                await db.commit()
                recorder = _TurnRecorder(
                    db, persisted_turn, identity,
                    usage_ledger_enabled=self.reliability_enabled,
                )
            try:
                async def observe_stream(event: StreamEvent) -> None:
                    if self.stream_observer is None or persisted_turn is None:
                        return
                    returned = self.stream_observer(persisted_turn.id, event)
                    if returned is not None:
                        await returned

                result = await self.adapter.invoke(
                    ProviderRequest(
                        messages=tuple(request_messages), tools=tuple(presented_definitions),
                        parameters=model_parameters,
                    ),
                    attempt_observer=recorder.observe if recorder else None,
                    stream_observer=observe_stream if self.stream_observer else None,
                )
            except ProviderError as error:
                if self.stream_observer is not None and persisted_turn is not None:
                    await observe_stream(StreamEvent(
                        type="reset", data={"reason_code": error.code},
                    ))
                if persisted_turn is not None:
                    persisted_turn.status = "failed"
                    persisted_turn.error_code = error.code
                    persisted_turn.ended_at = utcnow()
                    if self.reliability_enabled:
                        await record_progress(
                            db, run_id=identity.run_id,
                            work_item_id=identity.work_item_id,
                            runner_id=identity.runner_id,
                            agent_turn_id=persisted_turn.id,
                            made_progress=False,
                            payload={"provider_error": error.code},
                            reason_code="provider_error",
                        )
                    await db.flush()
                # A provider can reject a request despite a local token count.
                # Retry once before payload, after a validated semantic reduction.
                # Summary failure never replays committed Tool actions.
                if (
                    error.code == "provider_context_length"
                    and not error.received_payload
                    and not context_recovery_attempted
                    and turn_index < self.max_turns
                    and context.hard_input_limit is not None
                ):
                    context_recovery_attempted = True
                    if db is not None and db.in_transaction():
                        await db.commit()
                    try:
                        recovery = await fit_context_budget(
                            replace(context, messages=tuple(messages), manifest=tuple(source_manifest),
                                    source_messages=None, source_manifest=None,
                                    history_count=history_count, compaction=request_compaction or None),
                            provider=self.adapter.provider, provider_type=provider_type,
                            tools=presented_definitions, parameters=model_parameters,
                            invoke_summary=invoke_summary, force_compaction=True,
                        )
                    except HardCompactionError as compact_error:
                        raise AgentLoopError(str(compact_error), code=compact_error.code) from compact_error
                    request_compaction = dict(bind_compaction_scope(recovery.context, identity).compaction or {})
                    continue
                raise AgentLoopError(
                    f"Provider call failed: {error.code}", code=error.code,
                ) from error
            turns.append(result)
            if self.reliability_enabled:
                try:
                    budget_state = await assert_post_provider_budget(db, identity.run_id)
                except BudgetExceeded as error:
                    if self.stream_observer is not None and persisted_turn is not None:
                        await observe_stream(StreamEvent(
                            type="reset", data={"reason_code": error.code},
                        ))
                    if persisted_turn is not None:
                        persisted_turn.status = "failed"
                        persisted_turn.error_code = error.code
                        persisted_turn.ended_at = utcnow()
                        await record_progress(
                            db, run_id=identity.run_id,
                            work_item_id=identity.work_item_id,
                            runner_id=identity.runner_id,
                            agent_turn_id=persisted_turn.id,
                            made_progress=False,
                            payload={
                                "budget_dimension": error.dimension,
                                "used": error.used, "limit": error.limit,
                            },
                            reason_code="budget_exhausted",
                        )
                        await db.flush()
                    raise AgentLoopError(str(error), code=error.code) from error
            if persisted_turn is not None:
                persisted_turn.turn_items = [item.as_dict() for item in result.items]
                persisted_turn.stop_reason = result.stop_reason.value
                persisted_turn.usage = result.usage.as_dict()
                persisted_turn.provider_state_ref = result.provider_state_ref

            assistant_text = "".join(
                item.text for item in result.assistant_messages
            )
            tool_calls = list(result.tool_calls)
            invalid_stop_code = None
            if result.stop_reason == StopReason.MAX_TOKENS:
                invalid_stop_code = "provider_output_truncated"
            elif result.stop_reason == StopReason.CONTENT_FILTER:
                invalid_stop_code = "provider_content_filtered"
            elif result.stop_reason == StopReason.ERROR:
                invalid_stop_code = "provider_invalid_stop_reason"
            elif result.stop_reason == StopReason.TOOL_USE and not tool_calls:
                invalid_stop_code = "provider_invalid_stop_reason"
            elif not tool_calls and (
                result.stop_reason not in {StopReason.END_TURN, StopReason.UNKNOWN}
                or not result.assistant_messages
            ):
                invalid_stop_code = "provider_invalid_stop_reason"
            if invalid_stop_code is not None:
                if self.stream_observer is not None and persisted_turn is not None:
                    await observe_stream(StreamEvent(
                        type="reset", data={"reason_code": invalid_stop_code},
                    ))
                if persisted_turn is not None:
                    persisted_turn.status = "failed"
                    persisted_turn.error_code = invalid_stop_code
                    persisted_turn.ended_at = utcnow()
                    if self.reliability_enabled:
                        await record_progress(
                            db, run_id=identity.run_id,
                            work_item_id=identity.work_item_id,
                            runner_id=identity.runner_id,
                            agent_turn_id=persisted_turn.id,
                            made_progress=False,
                            payload={
                                "assistant_text_hash": hashlib.sha256(
                                    assistant_text.encode()
                                ).hexdigest(),
                                "stop_reason": result.stop_reason.value,
                                "error_code": invalid_stop_code,
                            },
                            reason_code=invalid_stop_code,
                        )
                    await db.flush()
                raise AgentLoopError(
                    f"Provider returned incomplete output: {result.stop_reason.value}",
                    code=invalid_stop_code,
                )
            if tool_calls:
                if self.stream_observer is not None and persisted_turn is not None:
                    await observe_stream(StreamEvent(
                        type="reset", data={"reason_code": "tool_use"},
                    ))
                messages.append({
                    "role": "assistant", "content": assistant_text,
                    "turn_items": [item.as_dict() for item in result.items],
                    "tool_calls": [{
                        "id": call.call_id, "type": "tool_use", "name": call.name,
                        "input": call.arguments,
                    } for call in tool_calls],
                })
                source_manifest = request_manifest(messages, source_manifest)
                waiting = False
                turn_schema_repairs = 0
                turn_dispatches: list[DispatchResult] = []
                batch_results = await self.dispatcher.dispatch_batch(
                    tool_calls, capability_snapshot=capability_snapshot,
                    runtime_context=runtime_context, db=db,
                    run_id=identity.run_id if identity else None,
                    work_item_id=identity.work_item_id if identity else None,
                    source_turn_id=persisted_turn.id if persisted_turn else None,
                )
                for call, dispatched in zip(tool_calls, batch_results, strict=True):
                    dispatches.append(dispatched)
                    turn_dispatches.append(dispatched)
                    provider_dispatch = dispatched
                    if call.name == "search_tools" and dispatched.result.success:
                        loaded = list((dispatched.result.data or {}).get("loaded_tool_definitions") or [])
                        effective = set(
                            capability_snapshot.effective_tool_keys
                            if isinstance(capability_snapshot, CapabilitySnapshot)
                            else capability_snapshot.get("effective_tool_keys", [])
                        )
                        existing_names = {item["name"] for item in presented_definitions}
                        admitted = [
                            item for item in loaded
                            if item.get("name") in effective and item.get("name") not in existing_names
                        ]
                        presented_definitions.extend(admitted)
                        self.dispatcher.add_definitions(admitted)
                        loaded_tool_version_ids.update(
                            item.get("tool_version_id") for item in admitted
                            if item.get("tool_version_id")
                        )
                        # Full frozen definitions are Runtime-internal and are
                        # presented as actual schemas on the next Turn. Avoid
                        # duplicating them into model context and checkpoints.
                        provider_dispatch = DispatchResult(
                            call_id=dispatched.call_id,
                            tool_name=dispatched.tool_name,
                            result=dispatched.result.model_copy(update={
                                "data": {
                                    "tools": (dispatched.result.data or {}).get("tools", []),
                                    "loaded_tool_definitions": [],
                                },
                            }),
                            invocation_id=dispatched.invocation_id,
                            durable_wait=dispatched.durable_wait,
                            schema_error=dispatched.schema_error,
                            wait_reason=dispatched.wait_reason,
                        )
                    if not dispatched.durable_wait:
                        messages.append(provider_dispatch.provider_message(project=False))
                        source_manifest.append({
                            "source": "continuation", "visibility": "private",
                            "tool_call_id": call.call_id,
                            "tool_invocation_id": dispatched.invocation_id,
                        })
                    waiting = waiting or dispatched.durable_wait
                    if dispatched.schema_error:
                        schema_repairs += 1
                        turn_schema_repairs += 1
                repair_exhausted = schema_repairs > self.max_schema_repairs
                if persisted_turn is not None:
                    persisted_turn.schema_repair_count = turn_schema_repairs
                    persisted_turn.status = (
                        "failed" if repair_exhausted
                        else "waiting_actions" if waiting
                        else "completed"
                    )
                    persisted_turn.error_code = (
                        "tool_schema_repair_exhausted"
                        if repair_exhausted else None
                    )
                    persisted_turn.ended_at = utcnow()
                    progress = None
                    if self.reliability_enabled:
                        progress = await record_progress(
                            db, run_id=identity.run_id,
                            work_item_id=identity.work_item_id,
                            runner_id=identity.runner_id,
                            agent_turn_id=persisted_turn.id,
                            made_progress=any(
                                item.invocation_id is not None
                                or (item.result.success and not item.schema_error)
                                for item in turn_dispatches
                            ),
                            payload={
                                "assistant_text_hash": hashlib.sha256(
                                    assistant_text.encode()
                                ).hexdigest(),
                                "dispatches": [
                                    {
                                        "tool": item.tool_name,
                                        "invocation_id": item.invocation_id,
                                        "status": item.result.status,
                                        "error_code": (
                                            item.result.error.code if item.result.error else None
                                        ),
                                    }
                                    for item in turn_dispatches
                                ],
                            },
                            reason_code=(
                                "tool_schema_repair_exhausted"
                                if repair_exhausted
                                else (
                                    "authoritative_action"
                                    if any(
                                        item.invocation_id is not None
                                        or (
                                            item.result.success
                                            and not item.schema_error
                                        )
                                        for item in turn_dispatches
                                    )
                                    else "no_authoritative_change"
                                )
                            ),
                        )
                    await db.flush()
                if repair_exhausted:
                    raise AgentLoopError(
                        "Tool schema repair budget exhausted",
                        code="tool_schema_repair_exhausted",
                    )
                if waiting:
                    return AgentLoopResult(
                        status="waiting_actions", final_message=None,
                        turns=tuple(turns), dispatches=tuple(dispatches),
                        messages=tuple(messages), final_stream_id=None,
                        continuation_messages=tuple(messages[continuation_start:]),
                        loaded_tool_version_ids=tuple(sorted(loaded_tool_version_ids)),
                        hard_compactions=tuple(request_compaction.get("hard_compactions") or []),
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
                        "loaded_tool_version_ids": sorted(loaded_tool_version_ids),
                        "hard_compactions": list(request_compaction.get("hard_compactions") or []),
                    }
                    presented_hash = hashlib.sha256(json.dumps([
                        {
                            "name": item["name"],
                            "input_schema": item.get("input_schema") or {},
                            "registration_hash": item.get("registration_hash"),
                        }
                        for item in presented_definitions
                    ], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
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
                        context_manifest={
                            "sections": list(message_manifest),
                            "omitted_sections": list(omitted_sections),
                            "compaction": request_compaction or None,
                        },
                        compaction_ref=request_compaction.get("content_hash"),
                        result_cursor={"agent_turn_id": persisted_turn.id if persisted_turn else None},
                        pending_refs={
                            "tool_invocation_ids": [
                                item.invocation_id for item in turn_dispatches
                                if item.invocation_id
                            ],
                        },
                        loaded_tool_version_ids=sorted(loaded_tool_version_ids),
                        presented_schema_bundle_hash=presented_hash,
                        content_hash=checkpoint_hash,
                        last_completed_step="tool_results_persisted",
                        safe_to_resume=True,
                    ))
                    await db.flush()
                    threshold = int(
                        (budget_state.limits if budget_state else {}).get(
                            "no_progress_turns", 4
                        )
                    )
                    if progress is not None and progress.consecutive_no_progress >= threshold:
                        messages.append({
                            "role": "system",
                            "content": (
                                "Runtime diagnostic: recent Turns produced no new durable fact. "
                                "Reassess the current evidence, change approach, ask for missing "
                                "information, or give a bounded factual report. Do not repeat the "
                                "same call merely to consume budget."
                            ),
                        })
                # Tool definitions intentionally remain unchanged here. A Tool
                # used successfully is still visible on the next model turn.
                continue

            if persisted_turn is not None:
                persisted_turn.status = "completed"
                persisted_turn.ended_at = utcnow()
                if self.reliability_enabled:
                    await record_progress(
                        db, run_id=identity.run_id,
                        work_item_id=identity.work_item_id,
                        runner_id=identity.runner_id,
                        agent_turn_id=persisted_turn.id,
                        made_progress=bool(assistant_text.strip()),
                        payload={
                            "assistant_text_hash": hashlib.sha256(
                                assistant_text.encode()
                            ).hexdigest(),
                            "stop_reason": result.stop_reason.value,
                        },
                        reason_code=(
                            "assistant_message" if assistant_text.strip()
                            else "empty_provider_result"
                        ),
                    )
                await db.flush()
            if result.assistant_messages:
                messages.append({"role": "assistant", "content": assistant_text})
                return AgentLoopResult(
                    status="completed", final_message=assistant_text,
                    turns=tuple(turns), dispatches=tuple(dispatches),
                    messages=tuple(messages),
                    final_stream_id=persisted_turn.id if persisted_turn else None,
                    hard_compactions=tuple(request_compaction.get("hard_compactions") or []),
                )
            raise AgentLoopError(
                "Provider returned no Tool Call and no assistant message",
                code="provider_invalid_stop_reason",
            )
        raise AgentLoopError("Agent turn budget exhausted", code="budget_exhausted")
