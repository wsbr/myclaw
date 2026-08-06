"""Durable Agent-native v2 Runner worker.

This worker is intentionally product-agnostic. Chat, future plan semantics, and
group collaboration may create WorkItems, but every executable WorkItem enters
the same SnapshotResolver -> ContextAssembler -> ModelAdapter -> AgentLoop ->
ActionDispatcher path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.core.model_hub.registry import ModelHub
from app.core.tool_registry.base import RuntimeContext
from app.database import AsyncSessionLocal
from app.models.common import utcnow, uuid7
from app.models.foundation import OutboxCommand, UserSession
from app.models.group_v1 import (
    GroupMembership,
    GroupRun,
    GroupRunMember,
    GroupSession,
    GroupSummaryRequest,
    SharedTask,
)
from app.models.runtime_v1 import (
    AgentInvocation,
    Approval,
    CapabilitySnapshot,
    Clarification,
    DurableRunner,
    Message,
    PlanUpdate,
    Run,
    RunnerCheckpoint,
    ToolInvocation,
    WorkItem,
    WorkingPlan,
    Workspace,
)
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_new_work_runner, persist_replacement_runner
from app.runtime import (
    ActionDispatcher,
    AgentLoopCoordinator,
    ContextAssembler,
    LoopIdentity,
    ModelAdapter,
    SnapshotResolver,
)
from app.services.provider_conformance import resolve_credential
from app.services.plan_mode_service import create_plan_approval
from app.services.runtime_resume_service import queue_work_item_resume
from app.services.working_plan_service import current_working_plan, working_plan_view


logger = logging.getLogger(__name__)


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

    @staticmethod
    async def _lock_event_session(db, session_id: str) -> UserSession:
        session = await db.scalar(select(UserSession).where(
            UserSession.id == session_id,
        ).execution_options(populate_existing=True).with_for_update())
        if session is None:
            raise LookupError(f"session not found: {session_id}")
        return session

    @staticmethod
    async def _next_group_message_sequence(db, group_session_id: str) -> int:
        # Message sequence is Group-wide (including follow-up Runs).  Lock the
        # Group aggregate before reading MAX so concurrent member settlements
        # cannot publish duplicate sequence numbers.
        await db.scalar(select(GroupSession.id).where(
            GroupSession.id == group_session_id,
        ).with_for_update())
        return int(await db.scalar(select(func.max(Message.sequence_no)).where(
            Message.group_session_id == group_session_id,
        )) or 0) + 1

    @staticmethod
    def _is_transient_settlement_conflict(error: Exception) -> bool:
        if not isinstance(error, DBAPIError):
            return False
        candidates = [getattr(error, "orig", None)]
        candidates.append(getattr(candidates[0], "__cause__", None))
        for candidate in candidates:
            args = getattr(candidate, "args", ())
            if args and args[0] in {1205, 1213}:
                return True
        return False

    async def _settle_with_retry(self, operation, *args) -> None:
        for attempt in range(3):
            try:
                await operation(*args)
                return
            except DBAPIError as error:
                if (
                    not self._is_transient_settlement_conflict(error)
                    or attempt == 2
                ):
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))

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
            invocation = await db.scalar(select(AgentInvocation).where(
                AgentInvocation.target_work_item_id == work.id,
                AgentInvocation.status == "queued",
            ).with_for_update())
            if invocation is not None:
                invocation.status = "running"
                invocation.version += 1
            if work.purpose == "group_summary" and work.group_run_id:
                summary_request = await db.scalar(select(GroupSummaryRequest).where(
                    GroupSummaryRequest.reporter_work_item_id == work.id,
                    GroupSummaryRequest.status == "queued",
                ).with_for_update())
                if summary_request is not None:
                    summary_request.status = "running"
                    summary_request.version += 1
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
                group_run = await db.get(GroupRun, work.group_run_id) if work.group_run_id else None
                group = await db.get(GroupSession, group_run.group_session_id) if group_run else None
                group_members: list[GroupRunMember] = []
                if group_run is not None:
                    group_members = list(await db.scalars(select(GroupRunMember).where(
                        GroupRunMember.group_run_id == group_run.id,
                        GroupRunMember.participation_status == "available",
                    ).order_by(GroupRunMember.agent_id)))
                working_plan, plan_update = await current_working_plan(
                    db, run_id=run.id, owner_agent_id=runner.agent_id,
                )
                working_plan_snapshot = (
                    working_plan_view(working_plan, plan_update)
                    if working_plan is not None else None
                )
                if run.mode == "plan" and resolved.capability.permission_mode == "plan_readonly":
                    platform_rules = (
                        "You are in Plan read-only permission mode. Investigate with read-only Tools, "
                        "ask necessary clarifications, and use discover_agents/invoke_agent only when "
                        "professional evidence would help. You MUST create or revise the visible "
                        "WorkingPlan with update_plan before presenting a plan. Never write, execute, "
                        "publish, or claim implementation succeeded. WorkingPlan is dynamic context/UI "
                        "state and never creates or schedules WorkItems."
                    )
                elif run.mode == "plan":
                    platform_rules = (
                        "The current WorkingPlan was approved and this same Plan Run now has direct "
                        "implementation permission. Implement dynamically with native Tools. Revise "
                        "the WorkingPlan when requirements or test evidence changes; do not treat it "
                        "as a frozen graph or fixed participant list. Choose collaborators on demand."
                    )
                elif run.mode == "group":
                    platform_rules = (
                        "You are one complete, peer Group member. No permanent Supervisor, role DAG, "
                        "or suggested edge controls your work. Use your own professional judgment. "
                        "Use send_group_message for direct or shared communication, invoke_agent for "
                        "bounded work that must return to you, search_group_history whenever prior Group "
                        "facts are needed, invite_agent when a real capability gap exists, and optional "
                        "SharedTask records only as a collaboration whiteboard. Directly question, review, "
                        "or send work back to any member when evidence requires it. A normal final message "
                        "ends only your current Turn; the persistent Coordinator and Reporter naturally "
                        "close the GroupRun after all pending work settles. Never claim another member acted "
                        "unless a real Message or AgentInvocation proves it."
                    )
                else:
                    platform_rules = (
                        "Use only the provided native Tool definitions. Tool results are authoritative. "
                        "A normal assistant message may finish the current turn. Use ask_user when "
                        "missing information prevents safe progress. Use update_plan only when a "
                        "multi-step task benefits from a visible working plan; simple tasks must not "
                        "be planned merely for ceremony. WorkingPlan never creates or schedules work."
                    )
                initial_message_id = (runner.input_snapshot or {}).get("message_id")
                if run.mode == "group" and group_run is not None:
                    context_history = [
                        item for item in history
                        if item.group_session_id == group_run.group_session_id
                        and item.status == "committed"
                        and (
                            item.visibility in {"group", "user"}
                            or item.sender_agent_id == runner.agent_id
                            or item.recipient_agent_id == runner.agent_id
                            or item.sender_type == "user"
                        )
                    ]
                    current_input = work.goal
                    current_input += "\n\nFixed GroupRun snapshot (data, not a workflow):\n" + json.dumps({
                        "group_session_id": group_run.group_session_id,
                        "group_run_id": group_run.id,
                        "reporter_agent_id": group_run.reporter_agent_id,
                        "summary_epoch": group_run.summary_epoch,
                        "shared_context": group_run.shared_context,
                        "members": [{
                            "agent_id": item.agent_id,
                            "agent_version_id": item.agent_version_id,
                            "membership_id": item.membership_id,
                            "capability_boundary": item.capability_boundary,
                            "is_current": item.agent_id == runner.agent_id,
                        } for item in group_members],
                    }, ensure_ascii=False, sort_keys=True)
                    if work.purpose == "group_summary":
                        current_input += (
                            "\n\nThis is the idempotent Reporter summary Turn. Faithfully report "
                            "delivered Artifacts, verified facts, failures, risks, and material "
                            "disagreements visible in Group history. Do not invent consensus."
                        )
                elif work.parent_work_item_id is None:
                    context_history = [
                        item for item in history
                        if item.run_id != run.id or item.id != initial_message_id
                    ]
                    current_input = work.goal
                else:
                    # A delegated Agent receives only its bounded request and
                    # explicit input snapshot, never the caller's ambient
                    # Session conversation.
                    context_history = []
                    explicit_context = (runner.input_snapshot or {}).get("context") or {}
                    current_input = work.goal
                    if explicit_context:
                        current_input += "\n\nExplicit invocation context:\n" + json.dumps(
                            explicit_context, ensure_ascii=False, sort_keys=True,
                        )
                context = ContextAssembler().assemble(
                    platform_rules=platform_rules,
                    agent_instructions=assembly.instructions,
                    current_input=current_input,
                    history=[
                        {"role": item.role if item.role != "tool_notice" else "system", "content": item.content}
                        for item in context_history
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
                    working_plan=working_plan_snapshot,
                    pending_results=list((runner.input_snapshot or {}).get("pending_results") or []),
                    token_budget=max(
                        1024, int((model_version.capabilities or {}).get("context_window", 24_000)) - 2048,
                    ),
                )
                runner.prompt_manifest = {
                    "sections": list(context.manifest), "estimated_tokens": context.estimated_tokens,
                    "compaction": context.compaction,
                    "working_plan_revision": (
                        working_plan.revision if working_plan is not None else None
                    ),
                }

            provider = ModelHub.get(
                {}, model_version.provider, resolve_credential(model_version.credential_ref),
                model_version.endpoint,
            )
            runtime_context = RuntimeContext(
                session_id=fence.session_id, runner_id=fence.runner_id,
                task_id=fence.work_item_id,
                user_id=str(runner.principal_snapshot.get("user_id") or "runtime"),
                account_id=runner.principal_snapshot.get("account_id"),
                agent_id=runner.agent_id, workspace_id=workspace_id,
                permission_mode=resolved.capability.permission_mode,
                permission_revision=int(
                    runner.principal_snapshot.get("permission_revision") or 1
                ),
                authentication_source=str(
                    runner.principal_snapshot.get("authentication_source") or "web_session"
                ),
                group_session_id=runner.principal_snapshot.get("group_session_id"),
                group_run_id=runner.principal_snapshot.get("group_run_id"),
                group_node_id=(
                    runner.principal_snapshot.get("group_membership_id")
                    or runner.principal_snapshot.get("group_node_id")
                ),
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
                await self._settle_with_retry(
                    self._settle_completed,
                    fence, result.final_message or "", context,
                )
            else:
                reasons = {dispatch.wait_reason for dispatch in result.dispatches if dispatch.durable_wait}
                wait_reason = (
                    "clarification" if "clarification" in reasons
                    else "approval" if "approval" in reasons
                    else "agent" if "agent" in reasons else "tool"
                )
                await self._settle_with_retry(
                    self._settle_waiting, fence, wait_reason, context, result,
                )
        except Exception as error:
            if self._is_transient_settlement_conflict(error):
                # The provider/tool dispatch transaction rolled back. Leave
                # the durable Runner fenced as running; its lease recovery
                # creates a replacement from the last committed checkpoint.
                logger.warning(
                    "Runtime transaction hit a transient lock conflict; lease recovery will resume",
                    extra={"runner_id": fence.runner_id, "run_id": fence.run_id},
                )
            else:
                await self._settle_with_retry(self._settle_failed, fence, error)
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
            session = await self._lock_event_session(db, fence.session_id)
            runner = await self._load_fenced_runner(db, fence)
            work = await db.scalar(select(WorkItem).where(
                WorkItem.id == fence.work_item_id,
                WorkItem.execution_epoch == fence.execution_epoch,
                WorkItem.active_runner_id == fence.runner_id,
            ).with_for_update())
            run = await db.scalar(select(Run).where(Run.id == fence.run_id).with_for_update())
            if work is None or run is None:
                raise RunnerFenced("WorkItem fencing rejected settlement")
            invocation = await db.scalar(select(AgentInvocation).where(
                AgentInvocation.target_work_item_id == work.id,
            ).with_for_update())
            if invocation is not None:
                await self._settle_agent_target(
                    db, runner=runner, work=work, run=run,
                    invocation=invocation, content=content,
                )
                return
            pending_tool = await db.scalar(select(ToolInvocation.id).where(
                ToolInvocation.work_item_id == work.id,
                ToolInvocation.status.in_((
                    "created", "waiting_confirmation", "waiting_resource",
                    "queued", "running", "cancel_requested",
                )),
            ).limit(1))
            pending_clarification = await db.scalar(select(Clarification.id).where(
                Clarification.work_item_id == work.id,
                Clarification.status == "open",
            ).limit(1))
            pending_approval = await db.scalar(select(Approval.id).where(
                Approval.work_item_id == work.id,
                Approval.status == "pending",
            ).limit(1))
            if pending_tool or pending_clarification or pending_approval:
                raise RunnerFenced("Run cannot complete while durable actions are pending")
            if (
                run.mode == "plan" and work.id == run.root_work_item_id
                and session.permission_mode == "plan_readonly"
            ):
                plan, update = await current_working_plan(
                    db, run_id=run.id, owner_agent_id=runner.agent_id,
                )
                if plan is None or update is None:
                    runner.status = "succeeded"
                    runner.ended_at = utcnow()
                    runner.lease_expires_at = None
                    runner.version += 1
                    work.active_runner_id = None
                    work.version += 1
                    required = await ExecutionEventWriter(db).append(
                        session_id=run.session_id, run_id=run.id,
                        event_type="plan.revision_required",
                        event_dedupe_key=f"runner:{runner.id}:working-plan-required",
                        actor_type="service", actor_id=self.worker_id,
                        entity_type="run", entity_id=run.id,
                        entity_version=run.version, visibility="user",
                        summary="A visible WorkingPlan is required before approval",
                        payload={"work_item_id": work.id}, correlation_id=run.id,
                    )
                    await queue_work_item_resume(
                        db, work=work, run=run, source_runner=runner,
                        pending_results=[{
                            "role": "system",
                            "content": (
                                "Before presenting the Plan, call update_plan with the current "
                                "multi-step WorkingPlan. Do not implement anything."
                            ),
                        }],
                        reason="working_plan_required",
                        caused_by_event_id=required.id,
                        actor_id=self.worker_id,
                    )
                    return
                await self._settle_plan_proposal(
                    db, runner=runner, work=work, run=run, session=session,
                    plan=plan, update=update, content=content, context=context,
                )
                return
            if run.mode == "group" and work.group_run_id:
                await self._settle_group_completed(
                    db, runner=runner, work=work, run=run,
                    content=content, context=context,
                )
                return
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
            plans = list(await db.scalars(select(WorkingPlan).where(
                WorkingPlan.run_id == run.id, WorkingPlan.status == "active",
            ).with_for_update()))
            for plan in plans:
                plan.status = "closed"
                plan.version += 1
            previous_checkpoint = await db.scalar(select(
                func.max(RunnerCheckpoint.sequence_no)
            ).where(RunnerCheckpoint.runner_id == runner.id)) or 0
            db.add(RunnerCheckpoint(
                id=uuid7(), runner_id=runner.id, work_item_id=work.id,
                sequence_no=int(previous_checkpoint) + 1,
                checkpoint_type="turn_completed",
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
                payload={"root_work_item_id": work.id, "outcome": "succeeded"},
                correlation_id=run.id,
                caused_by_event_id=event.id,
            )

    async def _group_pending_exists(
        self, db, *, group_run: GroupRun, exclude_work_item_id: str | None = None,
    ) -> bool:
        work_conditions = [
            WorkItem.group_run_id == group_run.id,
            WorkItem.status.in_((
                "queued", "running", "waiting_user", "waiting_approval",
                "waiting_agent", "waiting_tool", "waiting_resource",
            )),
        ]
        if exclude_work_item_id:
            work_conditions.append(WorkItem.id != exclude_work_item_id)
        if await db.scalar(select(WorkItem.id).where(*work_conditions).limit(1)):
            return True
        if await db.scalar(select(AgentInvocation.id).where(
            AgentInvocation.group_run_id == group_run.id,
            AgentInvocation.status.in_(("created", "queued", "running")),
        ).limit(1)):
            return True
        run_id = group_run.run_id
        if await db.scalar(select(Clarification.id).where(
            Clarification.run_id == run_id, Clarification.status == "open",
        ).limit(1)):
            return True
        if await db.scalar(select(Approval.id).where(
            Approval.run_id == run_id, Approval.status == "pending",
        ).limit(1)):
            return True
        return bool(await db.scalar(select(SharedTask.id).where(
            SharedTask.group_run_id == group_run.id,
            SharedTask.status == "in_progress",
            SharedTask.assignee_agent_id.is_not(None),
        ).limit(1)))

    async def _enqueue_group_quiescence(
        self, db, *, group_run: GroupRun, run: Run, cause_event_id: str,
    ) -> None:
        await OutboxWriter(db).enqueue(
            topic="runtime.control", action="evaluate_group_quiescence",
            aggregate_type="group_run", aggregate_id=group_run.id,
            partition_key=run.session_id,
            dedupe_key=f"group-quiescence:{cause_event_id}",
            payload_ref={
                "group_run_id": group_run.id,
                "cause_event_id": cause_event_id,
            },
        )

    async def _settle_group_completed(
        self, db, *, runner: DurableRunner, work: WorkItem, run: Run,
        content: str, context,
    ) -> None:
        group_run = await db.scalar(select(GroupRun).where(
            GroupRun.id == work.group_run_id,
        ).with_for_update())
        if group_run is None:
            raise RunnerFenced("GroupRun disappeared during settlement")
        summary_epoch = (runner.input_snapshot or {}).get("summary_epoch")
        is_summary = work.purpose == "group_summary" and summary_epoch is not None
        visibility = "user" if is_summary else "group"
        message = Message(
            id=uuid7(), session_id=run.session_id, user_id=work.user_id,
            scope_type="group", group_session_id=group_run.group_session_id,
            group_run_id=group_run.id, sender_type="agent",
            sender_agent_id=runner.agent_id,
            recipient_type="session" if is_summary else "group",
            visibility=visibility, role="assistant", content=content,
            content_format="markdown", status="committed", run_id=run.id,
            work_item_id=work.id,
            sequence_no=await self._next_group_message_sequence(
                db, group_run.group_session_id,
            ),
        )
        db.add(message)
        await db.flush()
        now = utcnow()
        runner.status = "succeeded"
        runner.result_message_id = message.id
        runner.ended_at = now
        runner.lease_expires_at = None
        runner.version += 1
        work.status = "succeeded"
        work.result_summary = content[:4000]
        work.result_data = {"message_id": message.id}
        work.active_runner_id = None
        work.version += 1
        created = await ExecutionEventWriter(db).append(
            session_id=run.session_id, run_id=run.id,
            group_session_id=group_run.group_session_id,
            group_run_id=group_run.id,
            event_type="group.message_created",
            event_dedupe_key=f"runner:{runner.id}:group-final-message",
            actor_type="agent", actor_id=runner.agent_id,
            entity_type="message", entity_id=message.id,
            visibility=visibility,
            summary="Reporter delivered Group summary" if is_summary else "Group member completed a Turn",
            payload={
                "message_id": message.id, "agent_id": runner.agent_id,
                "summary_epoch": summary_epoch,
            },
            correlation_id=run.id,
        )
        message.caused_by_event_id = created.id
        previous_checkpoint = await db.scalar(select(
            func.max(RunnerCheckpoint.sequence_no)
        ).where(RunnerCheckpoint.runner_id == runner.id)) or 0
        db.add(RunnerCheckpoint(
            id=uuid7(), runner_id=runner.id, work_item_id=work.id,
            sequence_no=int(previous_checkpoint) + 1,
            checkpoint_type="turn_completed",
            state_snapshot={
                "group_run_id": group_run.id,
                "summary_epoch": summary_epoch,
                "message_id": message.id,
            },
            execution_epoch=runner.execution_epoch,
            capability_snapshot_id=runner.capability_snapshot_id,
            context_manifest={"sections": list(context.manifest)},
            compaction_ref=(context.compaction or {}).get("content_hash"),
            last_completed_step="group_message_committed", safe_to_resume=False,
        ))
        if not is_summary:
            run.status = "active"
            run.lifecycle = "running"
            run.wait_reason = None
            run.version += 1
            await self._enqueue_group_quiescence(
                db, group_run=group_run, run=run, cause_event_id=created.id,
            )
            return

        request = await db.scalar(select(GroupSummaryRequest).where(
            GroupSummaryRequest.group_run_id == group_run.id,
            GroupSummaryRequest.summary_epoch == int(summary_epoch),
        ).with_for_update())
        if request is None or request.reporter_work_item_id != work.id:
            raise RunnerFenced("Summary epoch no longer owns Reporter settlement")
        if await self._group_pending_exists(
            db, group_run=group_run, exclude_work_item_id=work.id,
        ):
            request.status = "superseded"
            request.version += 1
            await self._enqueue_group_quiescence(
                db, group_run=group_run, run=run, cause_event_id=created.id,
            )
            return
        request.status = "delivered"
        request.final_message_id = message.id
        request.version += 1
        group_run.delivered_summary_epoch = int(summary_epoch)
        group_run.version += 1
        run.status = "completed"
        run.lifecycle = "completed"
        run.outcome = "succeeded"
        run.result_summary = content[:4000]
        run.completed_at = now
        run.finished_at = now
        run.wait_reason = None
        run.version += 1
        delivered = await ExecutionEventWriter(db).append(
            session_id=run.session_id, run_id=run.id,
            group_session_id=group_run.group_session_id,
            group_run_id=group_run.id,
            event_type="group.summary_delivered",
            event_dedupe_key=f"group-summary:{group_run.id}:epoch:{summary_epoch}:delivered",
            actor_type="agent", actor_id=runner.agent_id,
            entity_type="group_summary_request", entity_id=request.id,
            entity_version=request.version, visibility="user",
            summary="Group Reporter summary delivered",
            payload={"summary_epoch": summary_epoch, "message_id": message.id},
            correlation_id=run.id, caused_by_event_id=created.id,
        )
        await ExecutionEventWriter(db).append(
            session_id=run.session_id, run_id=run.id,
            group_session_id=group_run.group_session_id,
            group_run_id=group_run.id,
            event_type="run.completed",
            event_dedupe_key=f"run:{run.id}:group-summary:{summary_epoch}:completed",
            actor_type="service", actor_id="group-run-coordinator",
            entity_type="run", entity_id=run.id, entity_version=run.version,
            visibility="user", summary="GroupRun completed after Reporter delivery",
            payload={"summary_epoch": summary_epoch, "reporter_agent_id": runner.agent_id},
            correlation_id=run.id, caused_by_event_id=delivered.id,
        )

    async def _settle_plan_proposal(
        self, db, *, runner: DurableRunner, work: WorkItem, run: Run,
        session: UserSession, plan: WorkingPlan, update: PlanUpdate,
        content: str, context,
    ) -> None:
        message = Message(
            id=uuid7(), session_id=run.session_id, user_id=work.user_id,
            scope_type="chat", sender_type="agent", sender_agent_id=runner.agent_id,
            recipient_type="session", role="assistant", content=content,
            content_format="markdown", status="committed", run_id=run.id,
            work_item_id=work.id,
        )
        db.add(message)
        await db.flush()
        now = utcnow()
        runner.status = "succeeded"
        runner.result_message_id = message.id
        runner.ended_at = now
        runner.lease_expires_at = None
        runner.version += 1
        work.status = "waiting_approval"
        work.wait_reason = "waiting_approval"
        work.result_summary = content[:4000]
        work.active_runner_id = None
        work.version += 1
        run.status = "waiting_approval"
        run.lifecycle = "waiting"
        run.wait_reason = "waiting_approval"
        run.result_summary = content[:4000]
        run.version += 1
        created = await ExecutionEventWriter(db).append(
            session_id=run.session_id, run_id=run.id,
            event_type="message.created",
            event_dedupe_key=f"runner:{runner.id}:plan-proposal-message",
            actor_type="agent", actor_id=runner.agent_id,
            entity_type="message", entity_id=message.id, visibility="user",
            summary="Plan proposal created",
            payload={"message_id": message.id, "working_plan_id": plan.id,
                     "plan_revision": plan.revision},
            correlation_id=run.id,
        )
        message.caused_by_event_id = created.id
        approval = await create_plan_approval(
            db, session=session, run=run, work=work, plan=plan, update=update,
        )
        previous_checkpoint = await db.scalar(select(
            func.max(RunnerCheckpoint.sequence_no)
        ).where(RunnerCheckpoint.runner_id == runner.id)) or 0
        db.add(RunnerCheckpoint(
            id=uuid7(), runner_id=runner.id, work_item_id=work.id,
            sequence_no=int(previous_checkpoint) + 1,
            checkpoint_type="durable_wait",
            state_snapshot={
                "wait_reason": "waiting_approval",
                "working_plan_id": plan.id,
                "plan_revision": plan.revision,
                "approval_id": approval.id,
            },
            execution_epoch=runner.execution_epoch,
            capability_snapshot_id=runner.capability_snapshot_id,
            context_manifest={"sections": list(context.manifest)},
            compaction_ref=(context.compaction or {}).get("content_hash"),
            pending_refs={"approval_ids": [approval.id]},
            last_completed_step="plan_proposal_committed", safe_to_resume=True,
        ))
        await ExecutionEventWriter(db).append(
            session_id=run.session_id, run_id=run.id,
            event_type="run.waiting",
            event_dedupe_key=f"run:{run.id}:waiting:plan-revision:{plan.revision}",
            actor_type="service", actor_id=self.worker_id,
            entity_type="run", entity_id=run.id, entity_version=run.version,
            visibility="user", summary="Plan is waiting for user approval",
            payload={
                "status": run.status, "wait_reason": run.wait_reason,
                "working_plan_id": plan.id, "plan_revision": plan.revision,
                "approval_id": approval.id,
            },
            correlation_id=run.id, caused_by_event_id=created.id,
        )

    async def _resume_agent_batch_if_ready(
        self, db, *, run: Run, caller_work: WorkItem,
        caller_runner: DurableRunner, batch_key: str,
        caused_by_event_id: str,
    ) -> bool:
        invocations = list(await db.scalars(select(AgentInvocation).where(
            AgentInvocation.caller_work_item_id == caller_work.id,
            AgentInvocation.batch_key == batch_key,
        ).order_by(AgentInvocation.created_at, AgentInvocation.id)))
        terminal = {"succeeded", "failed", "cancelled", "timed_out", "rejected"}
        if not invocations or any(item.status not in terminal for item in invocations):
            return False
        if (
            caller_work.status != "waiting_agent"
            or caller_work.active_runner_id is not None
            or run.status in {"completed", "cancelled", "failed"}
        ):
            return False
        calls = []
        pending_results: list[dict] = []
        for item in invocations:
            call_id = str(
                (item.context_snapshot or {}).get("provider_call_id")
                or f"agent-invocation-{item.id}"
            )
            calls.append({
                "id": call_id, "type": "tool_use", "name": "invoke_agent",
                "input": {
                    "agent_id": item.target_agent_id,
                    "task": item.request_text,
                    "input": (item.context_snapshot or {}).get("input") or {},
                },
            })
            succeeded = item.status == "succeeded"
            result = {
                "status": "succeeded" if succeeded else "failed",
                "summary": item.result_summary or item.error_detail or (
                    "Invoked Agent completed" if succeeded else "Invoked Agent failed"
                ),
                "data": {
                    "agent_invocation_id": item.id,
                    "target_agent_id": item.target_agent_id,
                    **dict(item.result_data or {}),
                },
                "error_code": None if succeeded else (
                    item.error_code or "agent_invocation_failed"
                ),
                "error": None if succeeded else (
                    item.error_detail or "Invoked Agent failed"
                ),
            }
            pending_results.append({
                "role": "tool", "tool_use_id": call_id, "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            })
        pending_results.insert(0, {
            "role": "assistant", "content": "", "tool_calls": calls,
        })
        await queue_work_item_resume(
            db, work=caller_work, run=run, source_runner=caller_runner,
            pending_results=pending_results, reason="agent_results",
            caused_by_event_id=caused_by_event_id,
            actor_id="agent-invocation-gateway",
        )
        return True

    async def _settle_agent_target(
        self, db, *, runner: DurableRunner, work: WorkItem, run: Run,
        invocation: AgentInvocation, content: str | None = None,
        error: Exception | None = None,
    ) -> None:
        succeeded = error is None
        now = utcnow()
        detail = None if succeeded else str(error)[:4000]
        code = None if succeeded else str(
            getattr(error, "code", type(error).__name__)
        )[:100]
        runner.status = "succeeded" if succeeded else "failed"
        runner.failure_code = code
        runner.error_code = code
        runner.error_detail = detail
        runner.ended_at = now
        runner.lease_expires_at = None
        runner.version += 1
        work.status = "succeeded" if succeeded else "failed"
        work.result_summary = content[:4000] if content is not None else None
        work.result_data = {"content": content} if content is not None else None
        work.error_code = None if succeeded else "agent_invocation_failed"
        work.error_detail = detail
        work.retryable = not succeeded
        work.active_runner_id = None
        work.version += 1
        invocation.status = "succeeded" if succeeded else "failed"
        invocation.result_summary = work.result_summary
        invocation.result_data = work.result_data
        invocation.error_code = work.error_code
        invocation.error_detail = detail
        invocation.completed_at = now
        invocation.version += 1
        group_run = await db.get(GroupRun, invocation.group_run_id) if invocation.group_run_id else None
        events = ExecutionEventWriter(db)
        settled = await events.append(
            session_id=run.session_id, run_id=run.id,
            group_session_id=group_run.group_session_id if group_run else None,
            group_run_id=group_run.id if group_run else None,
            event_type=(
                "agent_invocation.succeeded" if succeeded
                else "agent_invocation.failed"
            ),
            event_dedupe_key=f"agent_invocation:{invocation.id}:settled:v{invocation.version}",
            actor_type="agent", actor_id=runner.agent_id,
            entity_type="agent_invocation", entity_id=invocation.id,
            entity_version=invocation.version, visibility="user",
            summary=(
                "Agent collaboration request completed" if succeeded
                else "Agent collaboration request failed"
            ),
            payload={
                "target_agent_id": invocation.target_agent_id,
                "status": invocation.status,
                "error_code": invocation.error_code,
            },
            correlation_id=run.id,
        )
        notice = Message(
            id=uuid7(), session_id=run.session_id, user_id=work.user_id,
            scope_type="group" if group_run else "chat",
            group_session_id=group_run.group_session_id if group_run else None,
            group_run_id=group_run.id if group_run else None,
            sender_type="agent", sender_agent_id=runner.agent_id,
            recipient_type="agent" if group_run else "session",
            recipient_agent_id=invocation.caller_agent_id if group_run else None,
            recipient_group_node_id=(
                (await db.get(WorkItem, invocation.caller_work_item_id)).group_node_id
                if group_run else None
            ),
            visibility="direct" if group_run else "user", role="tool_notice",
            content=(
                (
                    f"AgentInvocation {invocation.id} from Agent "
                    f"{invocation.target_agent_id} succeeded:\n{content or ''}"
                )
                if succeeded else (
                    f"AgentInvocation {invocation.id} from Agent "
                    f"{invocation.target_agent_id} failed safely: "
                    f"{detail or code or 'unknown error'}"
                )
            ),
            content_format="markdown", status="committed", run_id=run.id,
            work_item_id=work.id, caused_by_event_id=settled.id,
            sequence_no=(await self._next_group_message_sequence(
                db, group_run.group_session_id,
            )) if group_run else None,
        )
        db.add(notice)
        await db.flush()
        invocation.result_message_id = notice.id
        caller_work = await db.scalar(select(WorkItem).where(
            WorkItem.id == invocation.caller_work_item_id,
        ).with_for_update())
        caller_runner = await db.get(DurableRunner, invocation.caller_runner_id)
        if caller_work is not None and caller_runner is not None:
            await self._resume_agent_batch_if_ready(
                db, run=run, caller_work=caller_work,
                caller_runner=caller_runner,
                batch_key=str(invocation.batch_key or invocation.caller_runner_id),
                caused_by_event_id=settled.id,
            )
        if group_run is not None:
            await self._enqueue_group_quiescence(
                db, group_run=group_run, run=run, cause_event_id=settled.id,
            )

    async def _settle_waiting(self, fence: RunnerFence, reason: str, context, result) -> None:
        async with AsyncSessionLocal.begin() as db:
            await self._lock_event_session(db, fence.session_id)
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
            work.status = {
                "clarification": "waiting_user",
                "approval": "waiting_approval",
                "agent": "waiting_agent",
                "tool": "waiting_tool",
            }[reason]
            work.wait_reason = {
                "clarification": "waiting_user",
                "approval": "waiting_approval",
                "agent": "waiting_agent",
                "tool": "waiting_tool",
            }[reason]
            work.active_runner_id = None
            work.version += 1
            run.status = {
                "clarification": "waiting_user",
                "approval": "waiting_approval",
                "agent": "waiting_agent",
                "tool": "waiting_tool",
            }[reason]
            run.lifecycle = "waiting"
            run.wait_reason = run.status
            run.version += 1
            pending = list(await db.scalars(select(ToolInvocation.id).where(
                ToolInvocation.work_item_id == work.id,
                ToolInvocation.status.in_(("waiting_confirmation", "queued", "running")),
            )))
            pending_agents = list(await db.scalars(select(AgentInvocation.id).where(
                AgentInvocation.caller_work_item_id == work.id,
                AgentInvocation.batch_key == runner.id,
                AgentInvocation.status.in_(("created", "queued", "running")),
            )))
            continuation = []
            for message in reversed(result.messages):
                if message.get("role") == "assistant" and message.get("tool_calls"):
                    continuation = [dict(message)]
                    break
            previous_checkpoint = await db.scalar(select(
                func.max(RunnerCheckpoint.sequence_no)
            ).where(RunnerCheckpoint.runner_id == runner.id)) or 0
            db.add(RunnerCheckpoint(
                id=uuid7(), runner_id=runner.id, work_item_id=work.id,
                sequence_no=int(previous_checkpoint) + 1,
                checkpoint_type="durable_wait",
                state_snapshot={
                    "wait_reason": reason, "pending_results": continuation,
                },
                execution_epoch=fence.execution_epoch,
                capability_snapshot_id=runner.capability_snapshot_id,
                context_manifest={"sections": list(context.manifest)},
                compaction_ref=(context.compaction or {}).get("content_hash"),
                pending_refs={
                    "tool_invocation_ids": pending,
                    "agent_invocation_ids": pending_agents,
                },
                last_completed_step="durable_wait_committed", safe_to_resume=True,
            ))
            waiting_event = await ExecutionEventWriter(db).append(
                session_id=run.session_id, run_id=run.id,
                event_type="run.waiting",
                event_dedupe_key=f"run:{run.id}:waiting:epoch:{fence.execution_epoch}",
                actor_type="service", actor_id=self.worker_id,
                entity_type="run", entity_id=run.id, entity_version=run.version,
                visibility="user", summary=f"Run is {run.status}",
                payload={
                    "status": run.status, "wait_reason": run.wait_reason,
                    "work_item_id": work.id, "tool_invocation_ids": pending,
                    "agent_invocation_ids": pending_agents,
                }, correlation_id=run.id,
            )
            if reason == "agent":
                await self._resume_agent_batch_if_ready(
                    db, run=run, caller_work=work, caller_runner=runner,
                    batch_key=runner.id, caused_by_event_id=waiting_event.id,
                )
            queued_invocations = list(await db.scalars(select(ToolInvocation).where(
                ToolInvocation.work_item_id == work.id,
                ToolInvocation.status == "queued",
            ).with_for_update()))
            for invocation in queued_invocations:
                queued = await ExecutionEventWriter(db).append(
                    session_id=run.session_id, run_id=run.id,
                    event_type="tool_invocation.queued",
                    event_dedupe_key=f"tool_invocation:{invocation.id}:queued",
                    actor_type="service", actor_id=self.worker_id,
                    entity_type="tool_invocation", entity_id=invocation.id,
                    entity_version=invocation.version, visibility="user",
                    summary=f"Tool queued: {invocation.tool_key}",
                    payload={"tool_key": invocation.tool_key},
                    correlation_id=run.id, caused_by_event_id=waiting_event.id,
                )
                await OutboxWriter(db).enqueue(
                    topic="runtime.sandbox", action="execute_tool",
                    aggregate_type="tool_invocation", aggregate_id=invocation.id,
                    partition_key=run.session_id,
                    dedupe_key=f"event:{queued.id}:execute_tool",
                    payload_ref={"tool_invocation_id": invocation.id},
                )

    async def _settle_failed(self, fence: RunnerFence, error: Exception) -> None:
        if isinstance(error, RunnerFenced):
            return
        async with AsyncSessionLocal.begin() as db:
            await self._lock_event_session(db, fence.session_id)
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
            invocation = await db.scalar(select(AgentInvocation).where(
                AgentInvocation.target_work_item_id == fence.work_item_id,
            ).with_for_update())
            if invocation is not None and work is not None and run is not None:
                await self._settle_agent_target(
                    db, runner=runner, work=work, run=run,
                    invocation=invocation, error=error,
                )
                return
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
            group_run = (
                await db.scalar(select(GroupRun).where(
                    GroupRun.id == work.group_run_id,
                ).with_for_update())
                if work is not None and work.group_run_id else None
            )
            if group_run is not None and run is not None:
                is_summary = work.purpose == "group_summary"
                summary_epoch = (runner.input_snapshot or {}).get("summary_epoch")
                request = None
                if is_summary and summary_epoch is not None:
                    request = await db.scalar(select(GroupSummaryRequest).where(
                        GroupSummaryRequest.group_run_id == group_run.id,
                        GroupSummaryRequest.summary_epoch == int(summary_epoch),
                    ).with_for_update())
                    if request is not None:
                        request.status = "failed"
                        request.version += 1
                    reporter = await db.scalar(select(GroupRunMember).where(
                        GroupRunMember.group_run_id == group_run.id,
                        GroupRunMember.agent_id == runner.agent_id,
                    ).with_for_update())
                    if reporter is not None:
                        reporter.participation_status = "unavailable"
                    run.status = "waiting_user"
                    run.lifecycle = "waiting"
                    run.wait_reason = "waiting_user"
                else:
                    # A peer Turn failure is local to that Turn. The GroupRun
                    # remains live and can still converge through another peer
                    # and its Reporter; Group is not a fragile all-or-nothing DAG.
                    run.status = "active"
                    run.lifecycle = "running"
                    run.wait_reason = None
                run.version += 1
                failed = await ExecutionEventWriter(db).append(
                    session_id=run.session_id, run_id=run.id,
                    group_session_id=group_run.group_session_id,
                    group_run_id=group_run.id,
                    event_type=(
                        "group.reporter_unavailable" if is_summary
                        else "group.member_turn_failed"
                    ),
                    event_dedupe_key=f"runner:{runner.id}:group-failed",
                    actor_type="service", actor_id=self.worker_id,
                    entity_type=(
                        "group_summary_request" if request is not None else "work_item"
                    ),
                    entity_id=request.id if request is not None else work.id,
                    entity_version=request.version if request is not None else work.version,
                    visibility="user" if is_summary else "group",
                    summary=(
                        "Group Reporter is unavailable; user replacement is required"
                        if is_summary else "A Group member Turn failed safely"
                    ),
                    payload={
                        "agent_id": runner.agent_id,
                        "error_code": code,
                        "summary_epoch": summary_epoch,
                    },
                    correlation_id=run.id,
                )
                if not is_summary:
                    await self._enqueue_group_quiescence(
                        db, group_run=group_run, run=run,
                        cause_event_id=failed.id,
                    )
                return
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
            await self._lock_event_session(db, runner.session_id)
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
            pending_clarification = await db.scalar(select(Clarification).where(
                Clarification.work_item_id == work.id,
                Clarification.status == "open",
            ).limit(1))
            pending_approval = await db.scalar(select(Approval).where(
                Approval.work_item_id == work.id,
                Approval.status == "pending",
            ).limit(1))
            pending_tools = list(await db.scalars(select(ToolInvocation).where(
                ToolInvocation.work_item_id == work.id,
                ToolInvocation.status.in_((
                    "waiting_confirmation", "waiting_resource", "queued", "running",
                )),
            ).with_for_update()))
            pending_agents = list(await db.scalars(select(AgentInvocation).where(
                AgentInvocation.caller_work_item_id == work.id,
                AgentInvocation.status.in_(("created", "queued", "running")),
            ).with_for_update()))
            if pending_clarification or pending_approval or pending_tools or pending_agents:
                if pending_clarification:
                    status = "waiting_user"
                elif pending_approval:
                    status = "waiting_approval"
                elif pending_agents:
                    status = "waiting_agent"
                else:
                    status = "waiting_tool"
                work.status = status
                work.wait_reason = status
                work.version += 1
                run.status = status
                run.lifecycle = "waiting"
                run.wait_reason = status
                run.version += 1
                waiting = await ExecutionEventWriter(db).append(
                    session_id=run.session_id, run_id=run.id,
                    event_type="run.waiting",
                    event_dedupe_key=f"run:{run.id}:waiting:recovery:{runner.id}",
                    actor_type="service", actor_id=self.worker_id,
                    entity_type="run", entity_id=run.id,
                    entity_version=run.version, visibility="user",
                    summary=f"Recovered durable wait: {status}",
                    payload={"work_item_id": work.id, "status": status},
                    correlation_id=run.id,
                )
                for invocation in pending_tools:
                    if invocation.status not in {"queued", "waiting_resource"}:
                        continue
                    await OutboxWriter(db).enqueue(
                        topic="runtime.sandbox", action="execute_tool",
                        aggregate_type="tool_invocation", aggregate_id=invocation.id,
                        partition_key=run.session_id,
                        dedupe_key=f"event:{waiting.id}:tool:{invocation.id}",
                        payload_ref={"tool_invocation_id": invocation.id},
                    )
                return True
            settled_agent = await db.scalar(select(AgentInvocation.id).where(
                AgentInvocation.caller_work_item_id == work.id,
                AgentInvocation.batch_key == runner.id,
                AgentInvocation.status.in_((
                    "succeeded", "failed", "cancelled", "timed_out", "rejected",
                )),
            ).limit(1))
            if settled_agent is not None:
                work.status = "waiting_agent"
                work.wait_reason = "waiting_agent"
                work.version += 1
                run.status = "waiting_agent"
                run.lifecycle = "waiting"
                run.wait_reason = "waiting_agent"
                run.version += 1
                recovered = await ExecutionEventWriter(db).append(
                    session_id=run.session_id, run_id=run.id,
                    event_type="run.waiting",
                    event_dedupe_key=f"run:{run.id}:waiting:agent-recovery:{runner.id}",
                    actor_type="service", actor_id=self.worker_id,
                    entity_type="run", entity_id=run.id,
                    entity_version=run.version, visibility="user",
                    summary="Recovered settled Agent collaboration results",
                    payload={"work_item_id": work.id, "status": "waiting_agent"},
                    correlation_id=run.id,
                )
                await self._resume_agent_batch_if_ready(
                    db, run=run, caller_work=work, caller_runner=runner,
                    batch_key=runner.id, caused_by_event_id=recovered.id,
                )
                return True
            if checkpoint is None or run.status in {"completed", "cancelled", "failed"}:
                work.status = "failed"
                work.error_code = "runner_lost_without_safe_checkpoint"
                work.version += 1
                group_run = (
                    await db.scalar(select(GroupRun).where(
                        GroupRun.id == work.group_run_id,
                    ).with_for_update())
                    if work.group_run_id else None
                )
                if group_run is not None and run.status not in {
                    "completed", "cancelled", "failed",
                }:
                    is_summary = work.purpose == "group_summary"
                    summary_epoch = (runner.input_snapshot or {}).get("summary_epoch")
                    request = None
                    if is_summary and summary_epoch is not None:
                        request = await db.scalar(select(GroupSummaryRequest).where(
                            GroupSummaryRequest.group_run_id == group_run.id,
                            GroupSummaryRequest.summary_epoch == int(summary_epoch),
                        ).with_for_update())
                        if request is not None:
                            request.status = "failed"
                            request.version += 1
                        reporter = await db.scalar(select(GroupRunMember).where(
                            GroupRunMember.group_run_id == group_run.id,
                            GroupRunMember.agent_id == runner.agent_id,
                        ).with_for_update())
                        if reporter is not None:
                            reporter.participation_status = "unavailable"
                            reporter.version += 1
                        run.status = "waiting_user"
                        run.lifecycle = "waiting"
                        run.wait_reason = "waiting_user"
                    else:
                        run.status = "active"
                        run.lifecycle = "running"
                        run.wait_reason = None
                    run.version += 1
                    recovered = await ExecutionEventWriter(db).append(
                        session_id=run.session_id, run_id=run.id,
                        group_session_id=group_run.group_session_id,
                        group_run_id=group_run.id,
                        event_type=(
                            "group.reporter_unavailable" if is_summary
                            else "group.member_turn_failed"
                        ),
                        event_dedupe_key=f"runner:{runner.id}:group-recovery-failed",
                        actor_type="service", actor_id=self.worker_id,
                        entity_type=(
                            "group_summary_request" if request is not None else "work_item"
                        ),
                        entity_id=request.id if request is not None else work.id,
                        entity_version=request.version if request is not None else work.version,
                        visibility="user" if is_summary else "group",
                        summary=(
                            "Group Reporter was lost without a safe checkpoint"
                            if is_summary else "A Group member Turn was lost safely"
                        ),
                        payload={
                            "agent_id": runner.agent_id,
                            "error_code": "runner_lost_without_safe_checkpoint",
                            "summary_epoch": summary_epoch,
                        },
                        correlation_id=run.id,
                    )
                    if not is_summary:
                        await self._enqueue_group_quiescence(
                            db, group_run=group_run, run=run,
                            cause_event_id=recovered.id,
                        )
                    return True
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

    async def _evaluate_group_quiescence(
        self, db, *, group_run_id: str, trigger_event_id: str | None,
    ) -> None:
        # Settlement takes Run before GroupRun. Use the same order here to
        # avoid a cross-worker Run <-> GroupRun deadlock.
        visible_group_run = await db.get(GroupRun, group_run_id)
        if visible_group_run is None:
            return
        visible_run = await db.get(Run, visible_group_run.run_id)
        if visible_run is None:
            return
        await self._lock_event_session(db, visible_run.session_id)
        run = await db.scalar(select(Run).where(
            Run.id == visible_group_run.run_id,
        ).execution_options(populate_existing=True).with_for_update())
        group_run = await db.scalar(select(GroupRun).where(
            GroupRun.id == group_run_id,
        ).execution_options(populate_existing=True).with_for_update())
        if group_run is None:
            return
        if run is None or run.status in {"completed", "cancelled", "failed"}:
            return
        if run.cancellation_requested_at is not None:
            return
        if await self._group_pending_exists(db, group_run=group_run):
            return
        existing = await db.scalar(select(GroupSummaryRequest).where(
            GroupSummaryRequest.group_run_id == group_run.id,
            GroupSummaryRequest.status.in_(("queued", "running")),
        ).limit(1))
        if existing is not None:
            return
        reporter = await db.scalar(select(GroupRunMember).where(
            GroupRunMember.group_run_id == group_run.id,
            GroupRunMember.agent_id == group_run.reporter_agent_id,
            GroupRunMember.participation_status == "available",
        ))
        if reporter is None or not reporter.agent_version_id:
            run.status = "waiting_user"
            run.lifecycle = "waiting"
            run.wait_reason = "waiting_user"
            run.version += 1
            await ExecutionEventWriter(db).append(
                session_id=run.session_id, run_id=run.id,
                group_session_id=group_run.group_session_id,
                group_run_id=group_run.id,
                event_type="group.reporter_unavailable",
                event_dedupe_key=f"group-run:{group_run.id}:reporter-unavailable:v{group_run.version}",
                actor_type="service", actor_id="group-run-coordinator",
                entity_type="group_run", entity_id=group_run.id,
                entity_version=group_run.version, visibility="user",
                summary="Group Reporter requires user replacement",
                payload={"reporter_agent_id": group_run.reporter_agent_id},
                correlation_id=run.id,
            )
            return
        membership = await db.get(GroupMembership, reporter.membership_id)
        group = await db.get(GroupSession, group_run.group_session_id)
        session = await db.get(UserSession, run.session_id)
        if membership is None or group is None or session is None:
            raise RuntimeError("Reporter membership snapshot is incomplete")
        group_run.summary_epoch += 1
        group_run.version += 1
        epoch = group_run.summary_epoch
        work = WorkItem(
            id=uuid7(), run_id=run.id, session_id=run.session_id, user_id=run.user_id,
            mode="group", owner_agent_id=reporter.agent_id,
            agent_version_id=reporter.agent_version_id,
            group_run_id=group_run.id, group_node_id=membership.id,
            goal="Deliver the natural GroupRun summary from visible collaboration facts",
            purpose="group_summary", status="queued", execution_epoch=1, attempt_no=1,
        )
        runner = DurableRunner(
            id=uuid7(), work_item_id=work.id, run_id=run.id,
            session_id=run.session_id, agent_id=reporter.agent_id,
            agent_version_id=reporter.agent_version_id,
            execution_epoch=1, status="queued",
            principal_snapshot={
                "user_id": run.user_id, "account_id": session.account_id,
                "session_id": run.session_id,
                "entry_agent_id": group.initiator_agent_id or group.entry_agent_id,
                "current_agent_id": reporter.agent_id,
                "permission_mode": "direct",
                "permission_revision": session.permission_revision,
                "mode": "group", "group_session_id": group.id,
                "group_run_id": group_run.id,
                "group_membership_id": membership.id,
                "group_node_id": membership.id,
                "delegation_chain": [],
            },
            assembly_snapshot={"agent_version_id": reporter.agent_version_id},
            prompt_manifest={},
            input_snapshot={
                "group_session_id": group.id,
                "group_run_id": group_run.id,
                "group_membership_id": membership.id,
                "group_node_id": membership.id,
                "group_summary": True,
                "summary_epoch": epoch,
            },
        )
        await persist_new_work_runner(db, work, runner)
        request = GroupSummaryRequest(
            id=uuid7(), group_run_id=group_run.id, summary_epoch=epoch,
            reporter_work_item_id=work.id, trigger_event_id=trigger_event_id,
            status="queued",
        )
        db.add(request)
        run.status = "active"
        run.lifecycle = "running"
        run.wait_reason = None
        run.version += 1
        await db.flush()
        requested = await ExecutionEventWriter(db).append(
            session_id=run.session_id, run_id=run.id,
            group_session_id=group.id, group_run_id=group_run.id,
            event_type="group.summary_requested",
            event_dedupe_key=f"group-summary:{group_run.id}:epoch:{epoch}:requested",
            actor_type="service", actor_id="group-run-coordinator",
            entity_type="group_summary_request", entity_id=request.id,
            entity_version=request.version, visibility="group",
            summary="Group Reporter summary requested",
            payload={"summary_epoch": epoch, "reporter_agent_id": reporter.agent_id},
            correlation_id=run.id, caused_by_event_id=trigger_event_id,
        )
        queued = await ExecutionEventWriter(db).append(
            session_id=run.session_id, run_id=run.id,
            group_session_id=group.id, group_run_id=group_run.id,
            event_type="runner.queued",
            event_dedupe_key=f"group-summary:{group_run.id}:epoch:{epoch}:runner",
            actor_type="service", actor_id="group-run-coordinator",
            entity_type="runner", entity_id=runner.id,
            entity_version=runner.version, visibility="private",
            private_owner=("runner", runner.id),
            summary="Group Reporter Runner queued",
            payload={"summary_epoch": epoch, "reporter_agent_id": reporter.agent_id},
            correlation_id=run.id, caused_by_event_id=requested.id,
        )
        await OutboxWriter(db).enqueue(
            topic="runtime.runner", action="execute", aggregate_type="runner",
            aggregate_id=runner.id, partition_key=run.session_id,
            dedupe_key=f"event:{queued.id}:execute",
            payload_ref={
                "runner_id": runner.id, "group_run_id": group_run.id,
                "summary_epoch": epoch,
            },
        )

    async def process_one_group_control(self) -> bool:
        try:
            async with AsyncSessionLocal.begin() as db:
                command = await db.scalar(select(OutboxCommand).where(
                    OutboxCommand.topic == "runtime.control",
                    OutboxCommand.action == "evaluate_group_quiescence",
                    OutboxCommand.status.in_(("pending", "retry_wait")),
                    OutboxCommand.available_at <= utcnow(),
                ).order_by(OutboxCommand.priority.desc(), OutboxCommand.created_at)
                 .with_for_update(skip_locked=True).limit(1))
                if command is None:
                    return False
                command.status = "claimed"
                command.dispatch_lease_owner = self.worker_id
                command.dispatch_lease_token = uuid7()
                command.attempt_count += 1
                await self._evaluate_group_quiescence(
                    db,
                    group_run_id=str(command.payload_ref.get("group_run_id") or command.aggregate_id),
                    trigger_event_id=command.payload_ref.get("cause_event_id"),
                )
                command.status = "completed"
                command.completed_at = utcnow()
                command.dispatch_lease_owner = None
                command.dispatch_lease_token = None
                return True
        except Exception:
            # The transaction rolled back, leaving the command claimable. A
            # later poll or another worker retries the same idempotent command.
            logger.exception("Group quiescence command failed and will retry")
            return False

    async def run_forever(self, poll_seconds: float | None = None) -> None:
        interval = settings.runtime_worker_poll_seconds if poll_seconds is None else poll_seconds
        while True:
            try:
                if await self.recover_one_expired_runner():
                    continue
                if await self.process_one_group_control():
                    continue
                claim = await self.claim_one()
                if claim is None:
                    await asyncio.sleep(interval)
                    continue
                fence = await self.acquire_runner(claim)
                if fence is not None:
                    await self.execute(fence)
            except Exception:
                # Claims and Runner leases are durable. An unexpected poll
                # failure must not terminate the worker process; expired state
                # is reclaimed by the next loop or a peer replica.
                logger.exception("Runtime worker iteration failed; durable recovery will retry")
                await asyncio.sleep(interval)


async def main() -> None:
    await RuntimeWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(main())
