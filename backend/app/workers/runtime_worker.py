"""Durable Agent-native v2 Runner worker.

This worker is intentionally product-agnostic. Chat, future plan semantics, and
group collaboration may create WorkItems, but every executable WorkItem enters
the same SnapshotResolver -> ContextAssembler -> ModelAdapter -> AgentLoop ->
ActionDispatcher path.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select, update

from app.config import settings
from app.core.model_hub.registry import ModelHub
from app.core.tool_registry.base import RuntimeContext
from app.database import AsyncSessionLocal
from app.models.common import utcnow, uuid7
from app.models.foundation import OutboxCommand, UserSession
from app.models.runtime_v1 import (
    CapabilitySnapshot,
    DurableRunner,
    Message,
    Run,
    RunnerCheckpoint,
    ToolInvocation,
    WorkItem,
    Workspace,
)
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_replacement_runner
from app.runtime import (
    ActionDispatcher,
    AgentLoopCoordinator,
    ContextAssembler,
    LoopIdentity,
    ModelAdapter,
    SnapshotResolver,
)
from app.services.provider_conformance import resolve_credential


@dataclass(frozen=True)
class CommandClaim:
    command_id: str
    claim_token: str
    runner_id: str


@dataclass(frozen=True)
class RunnerFence:
    runner_id: str
    work_item_id: str
    run_id: str
    session_id: str
    execution_epoch: int
    lease_token: str


class RunnerFenced(RuntimeError):
    pass


class RuntimeWorker:
    def __init__(self, worker_id: str | None = None) -> None:
        self.worker_id = worker_id or f"runtime-v2:{os.getpid()}:{uuid7()}"

    async def claim_one(self) -> CommandClaim | None:
        async with AsyncSessionLocal.begin() as db:
            now = utcnow()
            expired = await db.scalar(
                select(OutboxCommand)
                .where(
                    OutboxCommand.topic == "runtime.runner",
                    OutboxCommand.status == "claimed",
                    OutboxCommand.dispatch_lease_expires_at < now,
                )
                .order_by(OutboxCommand.dispatch_lease_expires_at)
                .with_for_update(skip_locked=True).limit(1)
            )
            if expired is not None:
                expired.status = "retry_wait"
                expired.available_at = now
                expired.dispatch_lease_owner = None
                expired.dispatch_lease_token = None
                expired.dispatch_lease_expires_at = None
            command = await db.scalar(
                select(OutboxCommand)
                .where(
                    OutboxCommand.topic == "runtime.runner",
                    OutboxCommand.action == "execute",
                    OutboxCommand.status.in_(("pending", "retry_wait")),
                    OutboxCommand.available_at <= now,
                )
                .order_by(
                    OutboxCommand.priority.desc(), OutboxCommand.available_at,
                    OutboxCommand.created_at,
                )
                .with_for_update(skip_locked=True).limit(1)
            )
            if command is None:
                return None
            token = uuid7()
            command.status = "claimed"
            command.dispatch_lease_owner = self.worker_id
            command.dispatch_lease_token = token
            command.dispatch_lease_expires_at = now + timedelta(
                seconds=settings.runtime_outbox_claim_lease_seconds
            )
            command.attempt_count += 1
            return CommandClaim(
                command.id, token,
                str(command.payload_ref.get("runner_id") or command.aggregate_id),
            )

    async def acquire_runner(self, claim: CommandClaim) -> RunnerFence | None:
        async with AsyncSessionLocal.begin() as db:
            command = await db.scalar(select(OutboxCommand).where(
                OutboxCommand.id == claim.command_id,
                OutboxCommand.status == "claimed",
                OutboxCommand.dispatch_lease_token == claim.claim_token,
            ).with_for_update())
            if command is None:
                return None
            runner = await db.scalar(select(DurableRunner).where(
                DurableRunner.id == claim.runner_id,
            ).with_for_update())
            if runner is None:
                command.status = "completed"
                command.completed_at = utcnow()
                return None
            work = await db.scalar(select(WorkItem).where(
                WorkItem.id == runner.work_item_id,
            ).with_for_update())
            run = await db.scalar(select(Run).where(Run.id == runner.run_id).with_for_update())
            if (
                work is None or run is None or runner.status != "queued"
                or work.active_runner_id != runner.id
                or work.execution_epoch != runner.execution_epoch
                or run.status in {"completed", "cancelled", "failed"}
            ):
                command.status = "completed"
                command.completed_at = utcnow()
                return None
            now = utcnow()
            lease_token = uuid7()
            runner.status = "running"
            runner.lease_owner = self.worker_id
            runner.lease_token = lease_token
            runner.lease_expires_at = now + timedelta(
                seconds=settings.runtime_runner_lease_seconds
            )
            runner.heartbeat_at = now
            runner.started_at = now
            runner.fencing_token = runner.execution_epoch
            runner.version += 1
            work.status = "running"
            work.wait_reason = None
            work.version += 1
            command.status = "completed"
            command.completed_at = now
            command.dispatch_lease_expires_at = None
            db.add(RunnerCheckpoint(
                id=uuid7(), runner_id=runner.id, work_item_id=work.id,
                sequence_no=1, checkpoint_type="lease_acquired",
                state_snapshot={"execution_epoch": runner.execution_epoch},
                execution_epoch=runner.execution_epoch,
                last_completed_step="lease_acquired", safe_to_resume=True,
            ))
            await ExecutionEventWriter(db).append(
                session_id=runner.session_id, run_id=runner.run_id,
                event_type="runner.started",
                event_dedupe_key=f"runner:{runner.id}:epoch:{runner.execution_epoch}:started",
                actor_type="service", actor_id=self.worker_id,
                entity_type="runner", entity_id=runner.id, entity_version=runner.version,
                visibility="private", private_owner=("runner", runner.id),
                summary="Agent-native Runner started", payload={
                    "execution_epoch": runner.execution_epoch,
                }, correlation_id=runner.run_id,
            )
            return RunnerFence(
                runner.id, work.id, run.id, runner.session_id,
                runner.execution_epoch, lease_token,
            )

    async def heartbeat(self, fence: RunnerFence) -> bool:
        async with AsyncSessionLocal.begin() as db:
            now = utcnow()
            result = await db.execute(update(DurableRunner).where(
                DurableRunner.id == fence.runner_id,
                DurableRunner.status == "running",
                DurableRunner.execution_epoch == fence.execution_epoch,
                DurableRunner.lease_token == fence.lease_token,
            ).values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=settings.runtime_runner_lease_seconds),
                version=DurableRunner.version + 1,
            ))
            return result.rowcount == 1

    async def _heartbeat_loop(self, fence: RunnerFence, stop: asyncio.Event) -> None:
        interval = float(settings.runtime_runner_heartbeat_seconds)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                if not await self.heartbeat(fence):
                    return

    async def execute(self, fence: RunnerFence) -> None:
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_loop(fence, stop))
        try:
            async with AsyncSessionLocal.begin() as db:
                runner = await self._load_fenced_runner(db, fence)
                work = await db.get(WorkItem, fence.work_item_id)
                run = await db.get(Run, fence.run_id)
                session = await db.get(UserSession, fence.session_id)
                resolved = await SnapshotResolver().resolve(
                    db, runner=runner,
                    execution_principal=dict(runner.principal_snapshot or {}),
                    permission_mode=run.permission_mode or session.permission_mode,
                    hard_budget=dict(run.hard_budget_snapshot or {}),
                )
                capability_id = resolved.capability.id
                model_version = resolved.model_version
                tool_definitions = list(resolved.tool_definitions)
                assembly = resolved.assembly
                workspace_id = await db.scalar(select(Workspace.id).where(
                    Workspace.session_id == fence.session_id,
                ))
                history = list(await db.scalars(
                    select(Message).where(
                        Message.session_id == fence.session_id,
                        Message.status == "committed",
                    ).order_by(Message.created_at, Message.id)
                ))
                context = ContextAssembler().assemble(
                    platform_rules=(
                        "Use only the provided native Tool definitions. Tool results are authoritative. "
                        "A normal assistant message may finish the current turn."
                    ),
                    agent_instructions=assembly.instructions,
                    current_input=work.goal,
                    history=[
                        {"role": item.role if item.role != "tool_notice" else "system", "content": item.content}
                        for item in history if item.run_id != run.id
                    ],
                    permission_summary={
                        "mode": resolved.capability.permission_mode,
                        "effective_tool_keys": resolved.capability.effective_tool_keys,
                        "denied_capabilities": resolved.capability.denied_capabilities,
                    },
                    skill_sections=[{
                        "name": item.manifest_name, "version_id": item.id,
                        "checksum": item.checksum,
                        "content": json.dumps(item.manifest or {}, ensure_ascii=False),
                    } for item in resolved.skill_versions],
                    pending_results=list((runner.input_snapshot or {}).get("pending_results") or []),
                    token_budget=max(
                        1024, int((model_version.capabilities or {}).get("context_window", 24_000)) - 2048,
                    ),
                )
                runner.prompt_manifest = {
                    "sections": list(context.manifest), "estimated_tokens": context.estimated_tokens,
                    "compaction": context.compaction,
                }

            provider = ModelHub.get(
                {}, model_version.provider, resolve_credential(model_version.credential_ref),
                model_version.endpoint,
            )
            runtime_context = RuntimeContext(
                session_id=fence.session_id, runner_id=fence.runner_id,
                task_id=fence.work_item_id,
                user_id=str(runner.principal_snapshot.get("user_id") or "runtime"),
                agent_id=runner.agent_id, workspace_id=workspace_id,
            )
            dispatcher = ActionDispatcher(tool_definitions)
            async with AsyncSessionLocal() as db:
                capability = await db.get(CapabilitySnapshot, capability_id)
                result = await AgentLoopCoordinator(
                    ModelAdapter(provider), dispatcher,
                ).run(
                    context=context, tool_definitions=tool_definitions,
                    capability_snapshot=capability, runtime_context=runtime_context,
                    model_parameters={
                        **dict(model_version.default_parameters or {}),
                        "model_id": model_version.model_name,
                    },
                    db=db,
                    identity=LoopIdentity(
                        runner_id=fence.runner_id, work_item_id=fence.work_item_id,
                        run_id=fence.run_id, agent_version_id=assembly.id,
                        model_version_id=model_version.id, provider=model_version.provider,
                        execution_epoch=fence.execution_epoch,
                    ),
                )
                await db.commit()
            if result.status == "completed":
                await self._settle_completed(fence, result.final_message or "", context)
            else:
                wait_reason = "approval" if any(
                    dispatch.result.data and dispatch.result.data.get("status") == "waiting_confirmation"
                    for dispatch in result.dispatches
                ) else "tool"
                await self._settle_waiting(fence, wait_reason, context)
        except Exception as error:
            await self._settle_failed(fence, error)
        finally:
            stop.set()
            await heartbeat

    async def _load_fenced_runner(self, db, fence: RunnerFence) -> DurableRunner:
        runner = await db.scalar(select(DurableRunner).where(
            DurableRunner.id == fence.runner_id,
            DurableRunner.status == "running",
            DurableRunner.execution_epoch == fence.execution_epoch,
            DurableRunner.lease_token == fence.lease_token,
        ))
        if runner is None:
            raise RunnerFenced("Runner lease or epoch no longer owns writes")
        return runner

    async def _settle_completed(self, fence: RunnerFence, content: str, context) -> None:
        async with AsyncSessionLocal.begin() as db:
            runner = await self._load_fenced_runner(db, fence)
            work = await db.scalar(select(WorkItem).where(
                WorkItem.id == fence.work_item_id,
                WorkItem.execution_epoch == fence.execution_epoch,
                WorkItem.active_runner_id == fence.runner_id,
            ).with_for_update())
            run = await db.scalar(select(Run).where(Run.id == fence.run_id).with_for_update())
            if work is None or run is None:
                raise RunnerFenced("WorkItem fencing rejected settlement")
            message = Message(
                id=uuid7(), session_id=fence.session_id, user_id=work.user_id,
                scope_type="chat", sender_type="agent", sender_agent_id=runner.agent_id,
                recipient_type="session", role="assistant", content=content,
                content_format="markdown", status="committed", run_id=run.id,
                work_item_id=work.id,
            )
            db.add(message)
            await db.flush()
            runner.status = "succeeded"
            runner.result_message_id = message.id
            runner.ended_at = utcnow()
            runner.lease_expires_at = None
            runner.version += 1
            work.status = "succeeded"
            work.result_summary = content[:4000]
            work.active_runner_id = None
            work.version += 1
            run.status = "completed"
            run.lifecycle = "completed"
            run.outcome = "succeeded"
            run.result_summary = content[:4000]
            run.completed_at = runner.ended_at
            run.finished_at = runner.ended_at
            run.version += 1
            db.add(RunnerCheckpoint(
                id=uuid7(), runner_id=runner.id, work_item_id=work.id,
                sequence_no=2, checkpoint_type="turn_completed",
                state_snapshot={"run_status": "completed", "message_id": message.id},
                execution_epoch=fence.execution_epoch,
                capability_snapshot_id=runner.capability_snapshot_id,
                context_manifest={"sections": list(context.manifest)},
                compaction_ref=(context.compaction or {}).get("content_hash"),
                last_completed_step="assistant_message_committed",
                safe_to_resume=False,
            ))
            event = await ExecutionEventWriter(db).append(
                session_id=fence.session_id, run_id=run.id,
                event_type="message.created",
                event_dedupe_key=f"runner:{runner.id}:final-message",
                actor_type="agent", actor_id=runner.agent_id,
                entity_type="message", entity_id=message.id, visibility="user",
                summary="Agent response created", payload={"message_id": message.id},
                correlation_id=run.id,
            )
            message.caused_by_event_id = event.id
            await ExecutionEventWriter(db).append(
                session_id=fence.session_id, run_id=run.id,
                event_type="run.completed",
                event_dedupe_key=f"run:{run.id}:completed:v{run.version}",
                actor_type="service", actor_id=self.worker_id,
                entity_type="run", entity_id=run.id, entity_version=run.version,
                visibility="user", summary="Run completed",
                payload={"root_work_item_id": work.id}, correlation_id=run.id,
                caused_by_event_id=event.id,
            )

    async def _settle_waiting(self, fence: RunnerFence, reason: str, context) -> None:
        async with AsyncSessionLocal.begin() as db:
            runner = await self._load_fenced_runner(db, fence)
            work = await db.scalar(select(WorkItem).where(
                WorkItem.id == fence.work_item_id,
                WorkItem.execution_epoch == fence.execution_epoch,
                WorkItem.active_runner_id == fence.runner_id,
            ).with_for_update())
            run = await db.scalar(select(Run).where(Run.id == fence.run_id).with_for_update())
            if work is None or run is None:
                raise RunnerFenced("WorkItem fencing rejected wait transition")
            runner.status = "succeeded"
            runner.ended_at = utcnow()
            runner.lease_expires_at = None
            runner.version += 1
            work.status = "waiting_user" if reason == "approval" else "waiting_tool"
            work.wait_reason = reason
            work.active_runner_id = None
            work.version += 1
            run.status = "waiting_approval" if reason == "approval" else "waiting_tool"
            run.lifecycle = "waiting"
            run.wait_reason = "confirmation" if reason == "approval" else "tool"
            run.version += 1
            pending = list(await db.scalars(select(ToolInvocation.id).where(
                ToolInvocation.work_item_id == work.id,
                ToolInvocation.status.in_(("waiting_confirmation", "queued", "running")),
            )))
            db.add(RunnerCheckpoint(
                id=uuid7(), runner_id=runner.id, work_item_id=work.id,
                sequence_no=2, checkpoint_type="durable_wait",
                state_snapshot={"wait_reason": reason},
                execution_epoch=fence.execution_epoch,
                capability_snapshot_id=runner.capability_snapshot_id,
                context_manifest={"sections": list(context.manifest)},
                compaction_ref=(context.compaction or {}).get("content_hash"),
                pending_refs={"tool_invocation_ids": pending},
                last_completed_step="durable_wait_committed", safe_to_resume=True,
            ))

    async def _settle_failed(self, fence: RunnerFence, error: Exception) -> None:
        if isinstance(error, RunnerFenced):
            return
        async with AsyncSessionLocal.begin() as db:
            runner = await db.scalar(select(DurableRunner).where(
                DurableRunner.id == fence.runner_id,
                DurableRunner.status == "running",
                DurableRunner.execution_epoch == fence.execution_epoch,
                DurableRunner.lease_token == fence.lease_token,
            ).with_for_update())
            if runner is None:
                return
            work = await db.scalar(select(WorkItem).where(
                WorkItem.id == fence.work_item_id,
                WorkItem.active_runner_id == runner.id,
            ).with_for_update())
            run = await db.scalar(select(Run).where(Run.id == fence.run_id).with_for_update())
            code = getattr(error, "code", type(error).__name__)[:100]
            runner.status = "failed"
            runner.failure_code = code
            runner.error_code = code
            runner.error_detail = str(error)[:4000]
            runner.ended_at = utcnow()
            runner.lease_expires_at = None
            runner.version += 1
            if work is not None:
                work.status = "failed"
                work.error_code = code
                work.error_detail = str(error)[:4000]
                work.active_runner_id = None
                work.version += 1
            if run is not None:
                run.status = "failed"
                run.lifecycle = "completed"
                run.outcome = "failed"
                run.error_code = code
                run.completed_at = runner.ended_at
                run.finished_at = runner.ended_at
                run.version += 1
                await ExecutionEventWriter(db).append(
                    session_id=fence.session_id, run_id=run.id,
                    event_type="run.failed",
                    event_dedupe_key=f"run:{run.id}:failed:v{run.version}",
                    actor_type="service", actor_id=self.worker_id,
                    entity_type="run", entity_id=run.id, entity_version=run.version,
                    visibility="user", summary="Run failed safely",
                    payload={"error_code": code}, correlation_id=run.id,
                )

    async def recover_one_expired_runner(self) -> bool:
        async with AsyncSessionLocal.begin() as db:
            now = utcnow()
            runner = await db.scalar(select(DurableRunner).where(
                DurableRunner.status == "running",
                DurableRunner.lease_expires_at < now,
            ).order_by(DurableRunner.lease_expires_at).with_for_update(skip_locked=True).limit(1))
            if runner is None:
                return False
            work = await db.scalar(select(WorkItem).where(
                WorkItem.id == runner.work_item_id,
            ).with_for_update())
            run = await db.scalar(select(Run).where(Run.id == runner.run_id).with_for_update())
            runner.status = "lost"
            runner.failure_code = "runner_lease_expired"
            runner.ended_at = now
            runner.version += 1
            checkpoint = await db.scalar(select(RunnerCheckpoint).where(
                RunnerCheckpoint.runner_id == runner.id,
                RunnerCheckpoint.safe_to_resume.is_(True),
            ).order_by(RunnerCheckpoint.sequence_no.desc()).limit(1))
            if work is None or run is None or work.active_runner_id != runner.id:
                return True
            work.active_runner_id = None
            if checkpoint is None or run.status in {"completed", "cancelled", "failed"}:
                work.status = "failed"
                work.error_code = "runner_lost_without_safe_checkpoint"
                work.version += 1
                run.status = "failed"
                run.lifecycle = "completed"
                run.outcome = "failed"
                run.completed_at = now
                run.version += 1
                return True
            work.execution_epoch += 1
            work.attempt_no += 1
            work.status = "queued"
            work.version += 1
            replacement = DurableRunner(
                id=uuid7(), work_item_id=work.id, run_id=run.id,
                session_id=runner.session_id, agent_id=runner.agent_id,
                agent_version_id=runner.agent_version_id,
                execution_epoch=work.execution_epoch, fencing_token=work.execution_epoch,
                status="queued", principal_snapshot=dict(runner.principal_snapshot or {}),
                assembly_snapshot=dict(runner.assembly_snapshot or {}), prompt_manifest={},
                input_snapshot={
                    **dict(runner.input_snapshot or {}),
                    "recovered_from_checkpoint_id": checkpoint.id,
                    "pending_results": (checkpoint.state_snapshot or {}).get("pending_results", []),
                },
            )
            await persist_replacement_runner(db, work, replacement)
            event = await ExecutionEventWriter(db).append(
                session_id=runner.session_id, run_id=run.id,
                event_type="runner.recovered",
                event_dedupe_key=f"runner:{runner.id}:replacement:{replacement.id}",
                actor_type="service", actor_id=self.worker_id,
                entity_type="runner", entity_id=replacement.id,
                visibility="private", private_owner=("runner", replacement.id),
                summary="Replacement Runner queued from safe checkpoint",
                payload={"lost_runner_id": runner.id, "execution_epoch": work.execution_epoch},
                correlation_id=run.id,
            )
            await OutboxWriter(db).enqueue(
                topic="runtime.runner", action="execute", aggregate_type="runner",
                aggregate_id=replacement.id, partition_key=runner.session_id,
                dedupe_key=f"event:{event.id}:execute",
                payload_ref={"runner_id": replacement.id, "event_id": event.id},
            )
            return True

    async def run_forever(self, poll_seconds: float | None = None) -> None:
        interval = settings.runtime_worker_poll_seconds if poll_seconds is None else poll_seconds
        while True:
            if await self.recover_one_expired_runner():
                continue
            claim = await self.claim_one()
            if claim is None:
                await asyncio.sleep(interval)
                continue
            fence = await self.acquire_runner(claim)
            if fence is not None:
                await self.execute(fence)


async def main() -> None:
    await RuntimeWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(main())
