"""Transactional creation path for durable Chat work."""

from __future__ import annotations

from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, UserSession
from app.models.registry import SandboxProfile
from app.models.runtime_v1 import DurableRunner, Message, Run, SessionSandbox, Workspace, WorkItem
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_new_work_runner
from app.services.run_reliability_service import default_run_budget, deadline_from_budget


class AgentUnavailable(RuntimeError):
    pass


async def create_user_session(
    db: AsyncSession,
    *,
    user_id: str,
    entry_agent_id: str,
    origin_type: str = "web",
    origin_api_key_id: str | None = None,
    title: str | None = None,
    allow_unpublished: bool = False,
    agent_version_id: str | None = None,
) -> UserSession:
    agent = await db.get(AgentBlueprint, entry_agent_id)
    if agent is None or (
        not allow_unpublished
        and (agent.status != "published" or agent.current_published_version_id is None)
    ):
        raise AgentUnavailable("Agent is not available for new sessions")
    account_id = await db.scalar(select(PlatformAccount.id).where(
        PlatformAccount.user_id == user_id, PlatformAccount.status == "active",
    ))
    session = UserSession(
        id=uuid7(), user_id=user_id, account_id=account_id,
        entry_agent_id=entry_agent_id,
        status="active", title=title, origin_type=origin_type,
        origin_api_key_id=origin_api_key_id, next_event_sequence=1,
        permission_mode="direct", permission_revision=1,
        interaction_mode="chat", interaction_revision=1,
    )
    db.add(session)
    await db.flush()
    created = await ExecutionEventWriter(db).append(
        session_id=session.id, event_type="session.created",
        event_dedupe_key=f"transition:user_session:{session.id}:0:1:create",
        actor_type="user", actor_id=user_id, entity_type="user_session",
        entity_id=session.id, entity_version=session.version, visibility="user",
        summary="Session created", payload={"origin_type": origin_type},
        correlation_id=session.id,
    )
    selected_version_id = agent_version_id or agent.current_published_version_id
    version = await db.get(AgentAssemblyVersion, selected_version_id) if selected_version_id else None
    if version and version.sandbox_profile_id:
        profile = await db.get(SandboxProfile, version.sandbox_profile_id)
        if profile is None or not profile.enabled:
            raise AgentUnavailable("Agent Sandbox is unavailable")
        root_uri = f"workspace://{session.id}/"
        workspace_path = Path(settings.sandbox_root).resolve() / session.id
        workspace_path.mkdir(parents=True, exist_ok=False)
        (workspace_path / "agents" / agent.id).mkdir(parents=True, exist_ok=False)
        (workspace_path / ".myclaw" / "artifacts").mkdir(parents=True, exist_ok=False)
        sandbox = SessionSandbox(
            id=uuid7(), session_id=session.id, sandbox_profile_id=profile.id,
            profile_config_version=profile.config_version, status="ready", root_uri=root_uri,
        )
        workspace = Workspace(
            id=uuid7(), session_sandbox_id=sandbox.id, session_id=session.id,
            root_uri=root_uri, status="active", retention_policy_key="session_lifetime",
        )
        db.add_all([sandbox, workspace])
        await db.flush()
        await ExecutionEventWriter(db).append(
            session_id=session.id, event_type="sandbox.provisioned",
            event_dedupe_key=f"transition:session_sandbox:{sandbox.id}:0:1:provision",
            actor_type="service", actor_id="sandbox", entity_type="session_sandbox",
            entity_id=sandbox.id, entity_version=sandbox.version, visibility="user",
            summary="Session Sandbox is ready", payload={"type": profile.type},
            correlation_id=session.id, caused_by_event_id=created.id,
        )
    return session


async def enqueue_chat_message(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: str,
    content: str,
    idempotency_record_id: str,
    agent_version_id: str | None = None,
    execution_kind: str = "normal",
    mode: str | None = None,
) -> tuple[Message, Run, WorkItem, DurableRunner]:
    content = content.strip()
    if not content:
        raise ValueError("message content is required")
    session = await db.scalar(
        select(UserSession).where(UserSession.id == session_id).with_for_update()
    )
    if session is None or session.user_id != user_id:
        raise LookupError("session not found")
    if session.status != "active":
        raise RuntimeError("session is not active")
    agent = await db.get(AgentBlueprint, session.entry_agent_id)
    if agent is None:
        raise AgentUnavailable("Agent no longer exists")
    if execution_kind == "normal":
        if agent.status != "published" or agent.current_published_version_id is None:
            raise AgentUnavailable("Agent is not available for new runs")
        agent_version_id = agent.current_published_version_id
    elif agent_version_id is None:
        raise ValueError("draft test requires a fixed Agent version")

    resolved_mode = mode or (
        "plan" if session.permission_mode == "plan_readonly" else "chat"
    )
    if resolved_mode not in {"chat", "plan"}:
        raise ValueError("session messages support only chat or plan mode")

    message = Message(
        id=uuid7(), session_id=session.id, user_id=user_id, scope_type="chat",
        sender_type="user", sender_user_id=user_id, recipient_type="session",
        role="user", content=content, content_format="markdown", status="committed",
    )
    run_budget = default_run_budget(resolved_mode)
    run = Run(
        id=uuid7(), session_id=session.id, user_id=user_id, mode=resolved_mode,
        wait_reason=None, status="active", entry_agent_id=agent.id,
        permission_mode=session.permission_mode, hard_budget_snapshot=run_budget,
        deadline_at=deadline_from_budget(run_budget, mode=resolved_mode),
        started_at=utcnow(),
    )
    work = WorkItem(
        id=uuid7(), run_id=run.id, session_id=session.id, user_id=user_id,
        mode=resolved_mode, owner_agent_id=agent.id, agent_version_id=agent_version_id,
        goal=content, purpose="root_request", status="queued",
        resume_cursor={}, execution_epoch=1, attempt_no=1,
    )
    runner = DurableRunner(
        id=uuid7(), work_item_id=work.id, run_id=run.id, session_id=session.id,
        agent_id=agent.id, agent_version_id=agent_version_id, execution_epoch=1,
        fencing_token=1,
        status="queued", principal_snapshot={
            "account_id": session.account_id, "user_id": user_id,
            "session_id": session.id,
            "permission_revision": session.permission_revision,
            "authentication_source": (
                "agent_api_key" if session.origin_type == "api" else "web_session"
            ),
            "entry_agent_id": agent.id, "current_agent_id": agent.id,
            "mode": resolved_mode, "delegation_chain": [],
        }, assembly_snapshot={"agent_version_id": agent_version_id},
        prompt_manifest={}, input_snapshot={"message_id": message.id, "content": content},
    )
    message.run_id = run.id
    message.work_item_id = work.id
    # Run and Message reference each other through optional result/message
    # columns, while WorkItem.active_runner_id and Runner.work_item_id form a
    # deliberate cycle. Persist the durable chain explicitly, then backfill the
    # active Runner pointer. MySQL enforces these foreign keys even though
    # SQLite's default test setup does not.
    db.add(run)
    await db.flush()
    await persist_new_work_runner(db, work, runner)
    run.root_work_item_id = work.id
    db.add(message)
    await db.flush()

    events = ExecutionEventWriter(db)
    message_event = await events.append(
        session_id=session.id, run_id=run.id, event_type="message.created",
        event_dedupe_key=f"command:create_{resolved_mode}_run:{idempotency_record_id}:message",
        actor_type="user", actor_id=user_id, entity_type="message", entity_id=message.id,
        visibility="user", summary="User message created", payload={"message_id": message.id},
        correlation_id=run.id,
    )
    message.caused_by_event_id = message_event.id
    await events.append(
        session_id=session.id, run_id=run.id, event_type="run.created",
        event_dedupe_key=f"command:create_{resolved_mode}_run:{idempotency_record_id}:run",
        actor_type="user", actor_id=user_id, entity_type="run", entity_id=run.id,
        entity_version=run.version, visibility="user",
        summary=f"{resolved_mode.title()} run created",
        payload={"mode": resolved_mode}, correlation_id=run.id,
        caused_by_event_id=message_event.id,
    )
    runner_event = await events.append(
        session_id=session.id, run_id=run.id, event_type="runner.queued",
        event_dedupe_key=f"transition:runner:{runner.id}:0:1:queue",
        actor_type="service", actor_id="api", entity_type="runner", entity_id=runner.id,
        entity_version=runner.version, visibility="private", private_owner=("runner", runner.id),
        summary="Runner queued", payload={}, correlation_id=run.id,
        caused_by_event_id=message_event.id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.runner", action="execute", aggregate_type="runner",
        aggregate_id=runner.id, partition_key=session.id,
        dedupe_key=f"event:{runner_event.id}:execute",
        payload_ref={"runner_id": runner.id, "event_id": runner_event.id},
    )
    return message, run, work, runner
