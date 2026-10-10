"""Durable Provider attempt and usage recording shared by task and summary calls."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.common import utcnow, uuid7
from app.models.runtime_v1 import AgentTurn, ProviderAttempt
from app.core.model_hub.base import bounded_provider_id
from app.runtime.protocol import ProviderAttemptEvent, normalize_stop_reason
from app.services.usage_ledger_service import record_provider_attempt_usage

if TYPE_CHECKING:
    from app.runtime.agent_loop_coordinator import LoopIdentity


class TurnRecorder:
    def __init__(
        self, db: AsyncSession, turn: AgentTurn, identity: LoopIdentity,
        *, usage_ledger_enabled: bool,
    ) -> None:
        self.db = db
        self.turn = turn
        self.identity = identity
        self.usage_ledger_enabled = usage_ledger_enabled
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
            # Provider I/O must never inherit this transaction. Persist the
            # attempt start as a short fact transaction, then release the
            # connection before the SDK begins waiting for response bytes.
            await self.db.commit()
            return
        attempt = self.attempts[event.attempt_no]
        if event.phase == "metadata":
            request_id = bounded_provider_id(event.provider_request_id)
            if request_id:
                attempt.provider_request_id = request_id
            # Headers/lifecycle IDs do not start payload accounting or end the
            # attempt. Release this short transaction before reading more I/O.
            await self.db.commit()
            return
        if event.phase == "payload":
            attempt.received_payload = True
            if attempt.first_payload_at is None:
                attempt.first_payload_at = utcnow()
            await self.db.commit()
            return
        attempt.received_payload = event.received_payload
        attempt.ended_at = utcnow()
        if event.phase == "succeeded" and event.result is not None:
            attempt.status = "succeeded"
            attempt.tool_call_observed = bool(event.result.tool_calls)
            attempt.stop_reason = event.result.stop_reason.value
            attempt.usage = event.result.usage.as_dict()
            attempt.provider_request_id = bounded_provider_id(event.result.provider_request_id) or attempt.provider_request_id
            attempt.provider_state_ref = event.result.provider_state_ref
        else:
            error = event.error
            # Any provider payload makes transparent replay unsafe. Preserve
            # that uncertainty instead of disguising it as a clean transport
            # failure.
            attempt.status = "outcome_unknown" if event.received_payload else "failed"
            attempt.error_code = getattr(error, "code", None) or "provider_internal_error"
            attempt.error_detail = str(error)
            reported_usage = getattr(error, "usage", None)
            if reported_usage is not None:
                attempt.usage = reported_usage.as_dict()
            provider_stop_reason = getattr(error, "stop_reason", None)
            if provider_stop_reason is not None:
                attempt.stop_reason = normalize_stop_reason(provider_stop_reason).value
            attempt.provider_request_id = bounded_provider_id(getattr(error, "provider_request_id", None)) or attempt.provider_request_id
            diagnostics = getattr(error, "diagnostics", None)
            identifiers = {
                key: value for key, value in {
                    "provider_request_id": attempt.provider_request_id,
                    "provider_request_id_source": getattr(error, "provider_request_id_source", None),
                    "provider_response_id": bounded_provider_id(getattr(error, "provider_response_id", None)),
                }.items() if value is not None
            }
            if diagnostics or identifiers:
                detail = {
                    "message": str(error),
                    "provider_stop_reason": provider_stop_reason,
                }
                if diagnostics:
                    detail["diagnostics"] = diagnostics
                if identifiers:
                    detail["provider_identifiers"] = identifiers
                attempt.error_detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        if self.usage_ledger_enabled:
            await record_provider_attempt_usage(
                self.db,
                attempt=attempt,
                turn=self.turn,
                runner_id=self.identity.runner_id,
                work_item_id=self.identity.work_item_id,
                run_id=self.identity.run_id,
                agent_version_id=self.identity.agent_version_id,
            )
        await self.db.commit()
