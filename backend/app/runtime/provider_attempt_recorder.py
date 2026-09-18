"""Durable Provider attempt and usage recording shared by task and summary calls."""
from __future__ import annotations

from typing import TYPE_CHECKING
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.common import utcnow, uuid7
from app.models.runtime_v1 import AgentTurn, ProviderAttempt
from app.runtime.protocol import ProviderAttemptEvent
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
            attempt.provider_request_id = event.result.provider_request_id
            attempt.provider_state_ref = event.result.provider_state_ref
        else:
            error = event.error
            # Any provider payload makes transparent replay unsafe. Preserve
            # that uncertainty instead of disguising it as a clean transport
            # failure.
            attempt.status = "outcome_unknown" if event.received_payload else "failed"
            attempt.error_code = getattr(error, "code", type(error).__name__)
            attempt.error_detail = str(error)
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
