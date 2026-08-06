"""Dedicated durable worker for sandbox Tool invocations such as ``execute``."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select

from app.core.tool_registry.base import RuntimeContext, ToolResult
from app.core.tool_registry.registry import ToolRegistry
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.common import utcnow, uuid7
from app.models.foundation import OutboxCommand, UserSession
from app.models.group_v1 import GroupRun
from app.models.registry import PlatformTool
from app.models.runtime_v1 import (
    DurableRunner, Run, ToolAttempt, ToolInvocation, WorkItem, Workspace, WorkspaceLockRequest,
)
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.services.runtime_resume_service import (
    has_pending_actions, queue_work_item_resume, tool_continuation,
)
from app.services.artifact_service import reconcile_one_publishing_artifact
from app.services.workspace_lock_service import (
    WorkspaceLockConflict, WorkspaceLockGrant, acquire_workspace_lock,
    assert_workspace_lock, release_workspace_lock,
)


@dataclass(frozen=True)
class SandboxClaim:
    invocation_id: str
    attempt_id: str
    lease_token: str
    tool_key: str
    arguments: dict
    session_id: str
    run_id: str
    work_item_id: str
    source_runner_id: str
    user_id: str
    agent_id: str
    workspace_id: str | None
    lock_request_id: str | None
    workspace_lease_token: str | None
    workspace_fencing_token: int | None
    group_session_id: str | None
    group_run_id: str | None
    group_node_id: str | None
    preflight_error: str | None = None


class SandboxWorker:
    def __init__(self, worker_id: str | None = None):
        self.worker_id = worker_id or f"sandbox:{socket.gethostname()}:{os.getpid()}"

    async def claim_one(self) -> SandboxClaim | None:
        async with AsyncSessionLocal.begin() as db:
            now = utcnow()
            command = await db.scalar(select(OutboxCommand).where(
                OutboxCommand.topic == "runtime.sandbox",
                OutboxCommand.action == "execute_tool",
                or_(
                    (OutboxCommand.status.in_(("pending", "retry_wait"))) & (OutboxCommand.available_at <= now),
                    (OutboxCommand.status == "claimed") & (OutboxCommand.dispatch_lease_expires_at <= now),
                ),
            ).order_by(OutboxCommand.priority, OutboxCommand.created_at)
             .with_for_update(skip_locked=True).limit(1))
            if command is None:
                return None
            command.status = "claimed"
            command.attempt_count += 1
            command.dispatch_lease_owner = self.worker_id
            command.dispatch_lease_token = uuid7()
            command.dispatch_lease_expires_at = now + timedelta(
                seconds=settings.sandbox_outbox_claim_lease_seconds,
            )
            invocation = await db.scalar(select(ToolInvocation).where(
                ToolInvocation.id == command.aggregate_id,
            ).with_for_update())
            if invocation is None:
                command.status = "dead"
                command.last_error = "ToolInvocation does not exist"
                return None
            if invocation.status not in {"queued", "waiting_resource"}:
                self._complete_command(command)
                return None
            work = await db.scalar(select(WorkItem).where(WorkItem.id == invocation.work_item_id).with_for_update())
            run = await db.scalar(select(Run).where(Run.id == invocation.run_id).with_for_update())
            source_runner = await db.get(DurableRunner, invocation.runner_id)
            tool = await db.get(PlatformTool, invocation.platform_tool_id)
            if work is None or run is None or source_runner is None:
                command.status = "dead"
                command.last_error = "Durable Tool runtime graph is unavailable"
                return None
            if run.lifecycle in {"completed", "cancelled"} or work.status not in {
                "waiting_tool", "waiting_resource", "waiting_approval",
            }:
                invocation.status = "cancelled"
                invocation.error_code = "run_terminal"
                invocation.error_detail = "Run became terminal before durable Tool execution"
                invocation.version += 1
                self._complete_command(command)
                return None

            workspace = await db.scalar(select(Workspace).where(
                Workspace.session_id == run.session_id, Workspace.status == "active",
            ).with_for_update())
            lock_grant = None
            preflight_error = None
            workspace_access = str((tool.platform_config or {}).get("workspace_access") or "none") if tool else "none"
            if (
                tool is None or not tool.enabled
                or invocation.execution_mode != "durable_async"
                or tool.registration_hash != invocation.tool_registration_hash
            ):
                preflight_error = "Durable Tool is disabled or no longer registered"
            elif workspace_access in {"read", "write"}:
                if workspace is None:
                    preflight_error = "Tool requires an active Session Workspace"
                else:
                    existing = await db.scalar(select(WorkspaceLockRequest).where(
                        WorkspaceLockRequest.tool_invocation_id == invocation.id,
                        WorkspaceLockRequest.status.in_(("queued", "granted")),
                    ).order_by(WorkspaceLockRequest.ticket.desc()).with_for_update().limit(1))
                    if existing is not None and existing.status == "granted":
                        lock_grant = WorkspaceLockGrant(
                            request_id=existing.id, status=existing.status, mode=existing.mode,
                            ticket=existing.ticket, lease_token=existing.lease_token,
                            fencing_token=existing.fencing_token,
                        )
                    elif existing is None:
                        lock_grant = await acquire_workspace_lock(
                            db, workspace_id=workspace.id, tool_invocation_id=invocation.id,
                            mode=workspace_access,
                        )
                    if lock_grant is None or lock_grant.status == "queued":
                        invocation.status = "waiting_resource"
                        invocation.version += 1
                        command.status = "retry_wait"
                        command.available_at = now + timedelta(seconds=1)
                        command.dispatch_lease_owner = None
                        command.dispatch_lease_token = None
                        command.dispatch_lease_expires_at = None
                        return None

            invocation.status = "running"
            invocation.attempt_count += 1
            invocation.version += 1
            lease_token = uuid7()
            attempt = ToolAttempt(
                id=uuid7(), tool_invocation_id=invocation.id,
                attempt_no=invocation.attempt_count, status="running",
                worker_id=self.worker_id, lease_token=lease_token,
                request_snapshot={"arguments": invocation.arguments},
            )
            db.add(attempt)
            await ExecutionEventWriter(db).append(
                session_id=run.session_id, run_id=run.id, event_type="tool.attempt_started",
                event_dedupe_key=f"transition:tool_invocation:{invocation.id}:attempt:{attempt.attempt_no}:start",
                actor_type="service", actor_id=self.worker_id, entity_type="tool_attempt",
                entity_id=attempt.id, visibility="private", private_owner=("tool_invocation", invocation.id),
                summary=f"Durable Tool attempt started: {invocation.tool_key}",
                payload={"tool_key": invocation.tool_key, "attempt_no": attempt.attempt_no},
                correlation_id=run.id,
            )
            self._complete_command(command)
            group_session_id = None
            if work.group_run_id:
                group_run = await db.get(GroupRun, work.group_run_id)
                group_session_id = group_run.group_session_id if group_run else None
            return SandboxClaim(
                invocation_id=invocation.id, attempt_id=attempt.id, lease_token=lease_token,
                tool_key=invocation.tool_key, arguments=dict(invocation.arguments),
                session_id=run.session_id, run_id=run.id, work_item_id=work.id,
                source_runner_id=source_runner.id,
                user_id=str(source_runner.principal_snapshot.get("user_id") or run.user_id),
                agent_id=source_runner.agent_id, workspace_id=workspace.id if workspace else None,
                lock_request_id=lock_grant.request_id if lock_grant else None,
                workspace_lease_token=lock_grant.lease_token if lock_grant else None,
                workspace_fencing_token=lock_grant.fencing_token if lock_grant else None,
                group_session_id=group_session_id, group_run_id=work.group_run_id,
                group_node_id=work.group_node_id, preflight_error=preflight_error,
            )

    async def execute(self, claim: SandboxClaim) -> None:
        if claim.preflight_error:
            result = ToolResult(
                status="failed", error=claim.preflight_error,
                error_code="sandbox_preflight_failed", error_category="permanent",
            )
        else:
            context = RuntimeContext(
                session_id=claim.session_id, runner_id=claim.source_runner_id,
                task_id=claim.work_item_id, user_id=claim.user_id, agent_id=claim.agent_id,
                workspace_id=claim.workspace_id, workspace_lock_request_id=claim.lock_request_id,
                workspace_lease_token=claim.workspace_lease_token,
                workspace_fencing_token=claim.workspace_fencing_token,
                tool_invocation_id=claim.invocation_id,
                group_session_id=claim.group_session_id, group_run_id=claim.group_run_id,
                group_node_id=claim.group_node_id,
            )
            async with AsyncSessionLocal() as tool_db:
                result = await ToolRegistry.execute(claim.tool_key, claim.arguments, context, db=tool_db)
        await self.settle(claim, result)

    async def settle(self, claim: SandboxClaim, result: ToolResult) -> None:
        normalized = json.loads(json.dumps(result.data, ensure_ascii=False, default=str)) if result.data is not None else None
        envelope = result.model_copy(update={"data": normalized}).model_dump(mode="json")
        async with AsyncSessionLocal.begin() as db:
            await db.scalar(select(UserSession).where(UserSession.id == claim.session_id).with_for_update())
            invocation = await db.scalar(select(ToolInvocation).where(
                ToolInvocation.id == claim.invocation_id, ToolInvocation.status == "running",
            ).with_for_update())
            attempt = await db.scalar(select(ToolAttempt).where(
                ToolAttempt.id == claim.attempt_id, ToolAttempt.status == "running",
                ToolAttempt.lease_token == claim.lease_token,
            ).with_for_update())
            work = await db.scalar(select(WorkItem).where(WorkItem.id == claim.work_item_id).with_for_update())
            run = await db.scalar(select(Run).where(Run.id == claim.run_id).with_for_update())
            if invocation is None or attempt is None or work is None or run is None:
                return
            lock_valid = True
            if claim.lock_request_id:
                try:
                    await assert_workspace_lock(
                        db, request_id=claim.lock_request_id,
                        lease_token=claim.workspace_lease_token,
                        fencing_token=claim.workspace_fencing_token,
                    )
                except WorkspaceLockConflict:
                    lock_valid = False
            if not lock_valid:
                result = ToolResult(
                    status="failed", error="Workspace lease expired before Tool result was committed",
                    error_code="workspace_lease_lost", error_category="outcome_unknown",
                )
                envelope = result.model_copy(update={"data": normalized}).model_dump(mode="json")
                attempt.status = "outcome_unknown"
                invocation.status = "outcome_unknown"
            elif result.success:
                attempt.status = "succeeded"
                invocation.status = "succeeded"
            elif result.error and result.error.category == "outcome_unknown":
                attempt.status = "outcome_unknown"
                invocation.status = "outcome_unknown"
            else:
                attempt.status = "failed"
                invocation.status = "queued" if result.retryable and invocation.attempt_count < 3 else "failed"
            attempt.response_snapshot = envelope
            attempt.error_code = result.error.code if result.error else None
            attempt.error_detail = result.error.message if result.error else None
            attempt.retryable = result.retryable
            invocation.result_data = envelope
            invocation.error_code = attempt.error_code
            invocation.error_detail = attempt.error_detail
            invocation.version += 1
            event_type = ("tool.succeeded" if invocation.status == "succeeded"
                          else "tool.outcome_unknown" if invocation.status == "outcome_unknown"
                          else "tool.retry_scheduled" if invocation.status == "queued"
                          else "tool.failed")
            private_retry = invocation.status == "queued"
            settled = await ExecutionEventWriter(db).append(
                session_id=run.session_id, run_id=run.id, event_type=event_type,
                event_dedupe_key=f"transition:tool_invocation:{invocation.id}:attempt:{attempt.attempt_no}:settle",
                actor_type="service", actor_id=self.worker_id, entity_type="tool_invocation",
                entity_id=invocation.id, entity_version=invocation.version,
                visibility="private" if private_retry else "user",
                private_owner=("tool_invocation", invocation.id) if private_retry else None,
                summary=f"Durable Tool {invocation.status}: {invocation.tool_key}",
                payload={"tool_key": invocation.tool_key, "status": invocation.status}, correlation_id=run.id,
            )
            granted = []
            if claim.lock_request_id and lock_valid:
                _, granted = await release_workspace_lock(
                    db, request_id=claim.lock_request_id,
                    lease_token=claim.workspace_lease_token,
                    fencing_token=claim.workspace_fencing_token,
                )
            if granted:
                from app.workers.runtime_worker_v1 import RuntimeWorker
                await RuntimeWorker(f"{self.worker_id}:lock-release")._resume_workspace_grants(
                    db, granted, caused_by_event_id=settled.id,
                )
            if invocation.status == "queued":
                await OutboxWriter(db).enqueue(
                    topic="runtime.sandbox", action="execute_tool",
                    aggregate_type="tool_invocation", aggregate_id=invocation.id,
                    partition_key=run.session_id,
                    dedupe_key=f"event:{settled.id}:retry_tool",
                    payload_ref={"tool_invocation_id": invocation.id},
                    available_at=utcnow() + timedelta(seconds=2 ** invocation.attempt_count),
                )
                return
            if run.lifecycle in {"completed", "cancelled"} or work.status not in {
                "waiting_tool", "waiting_resource", "waiting_approval",
            }:
                return
            if await has_pending_actions(db, work_item_id=work.id):
                return
            await self._resume_work(db, invocation, work, run, settled.id)

    async def recover_one(self, stale_seconds: int = 60) -> bool:
        """Fence an abandoned provider attempt as outcome_unknown and resume."""
        claim = None
        async with AsyncSessionLocal.begin() as db:
            attempt = await db.scalar(select(ToolAttempt).where(
                ToolAttempt.status == "running",
                ToolAttempt.updated_at <= utcnow() - timedelta(seconds=stale_seconds),
            ).order_by(ToolAttempt.updated_at).with_for_update(skip_locked=True).limit(1))
            if attempt is None:
                return False
            invocation = await db.get(ToolInvocation, attempt.tool_invocation_id)
            work = await db.get(WorkItem, invocation.work_item_id) if invocation else None
            run = await db.get(Run, invocation.run_id) if invocation else None
            source = await db.get(DurableRunner, invocation.runner_id) if invocation else None
            if invocation is None or work is None or run is None or source is None:
                attempt.status = "outcome_unknown"
                attempt.error_code = "sandbox_attempt_abandoned"
                attempt.error_detail = "Durable Tool runtime graph is unavailable"
                return True
            workspace = await db.scalar(select(Workspace).where(Workspace.session_id == run.session_id))
            request = await db.scalar(select(WorkspaceLockRequest).where(
                WorkspaceLockRequest.tool_invocation_id == invocation.id,
                WorkspaceLockRequest.status == "granted",
            ).order_by(WorkspaceLockRequest.ticket.desc()).limit(1))
            group_session_id = None
            if work.group_run_id:
                group_run = await db.get(GroupRun, work.group_run_id)
                group_session_id = group_run.group_session_id if group_run else None
            claim = SandboxClaim(
                invocation_id=invocation.id, attempt_id=attempt.id,
                lease_token=attempt.lease_token, tool_key=invocation.tool_key,
                arguments=dict(invocation.arguments), session_id=run.session_id,
                run_id=run.id, work_item_id=work.id, source_runner_id=source.id,
                user_id=str(source.principal_snapshot.get("user_id") or run.user_id),
                agent_id=source.agent_id, workspace_id=workspace.id if workspace else None,
                lock_request_id=request.id if request else None,
                workspace_lease_token=request.lease_token if request else None,
                workspace_fencing_token=request.fencing_token if request else None,
                group_session_id=group_session_id, group_run_id=work.group_run_id,
                group_node_id=work.group_node_id,
            )
        await self.settle(claim, ToolResult(
            status="failed", error="Sandbox worker stopped after process execution may have started",
            error_code="sandbox_attempt_abandoned", error_category="outcome_unknown",
        ))
        return True

    async def _resume_work(self, db, invocation: ToolInvocation, work: WorkItem, run: Run, cause_id: str) -> None:
        source = await db.get(DurableRunner, invocation.runner_id)
        pending = await tool_continuation(
            db, work_item_id=work.id, source_turn_id=invocation.source_turn_id,
        )
        await queue_work_item_resume(
            db, work=work, run=run, source_runner=source,
            pending_results=pending, reason="tool_result",
            caused_by_event_id=cause_id, actor_id=self.worker_id,
        )

    async def run_forever(self, poll_seconds: float | None = None) -> None:
        poll_seconds = settings.sandbox_worker_poll_seconds if poll_seconds is None else poll_seconds
        while True:
            async with AsyncSessionLocal.begin() as db:
                if await reconcile_one_publishing_artifact(db):
                    continue
            if await self.recover_one(stale_seconds=settings.sandbox_attempt_stale_seconds):
                continue
            claim = await self.claim_one()
            if claim is None:
                await asyncio.sleep(poll_seconds)
                continue
            await self.execute(claim)

    @staticmethod
    def _complete_command(command: OutboxCommand) -> None:
        command.status = "completed"
        command.completed_at = utcnow()
        command.dispatch_lease_owner = None
        command.dispatch_lease_token = None
        command.dispatch_lease_expires_at = None


async def main() -> None:
    await SandboxWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(main())
