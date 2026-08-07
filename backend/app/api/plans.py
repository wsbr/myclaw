from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.sessions import _idempotency, _owned_session
from app.database import get_db
from app.deps import SessionActor, get_session_actor
from app.models.agent_v1 import AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import UserSession
from app.models.plan_v1 import PlanDeclaredAgent, PlanExecution, PlanWorkItemDependency, PlanWorkItemNode
from app.models.runtime_v1 import (
    AgentInvocation, DurableRunner, Message, Run, ToolConfirmation, ToolInvocation, WorkItem,
)
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.runtime_graph import persist_new_work_runner
from app.services.notification_delivery_service import create_notification_delivery_if_channel
from app.schemas.plan_v1 import (
    PlanConfirmRequest, PlanCreateRequest, PlanDependencyInput, PlanNodeInput, PlanPatchRequest,
    PlanReviseRequest,
)
from app.schemas.runtime_v1 import RunCancelRequest
from app.services.plan_service import (
    PlanValidationError, frozen_hash, graph_view, plan_view, replace_draft_graph, validate_graph,
)
from app.services.run_reliability_service import default_run_budget, deadline_from_budget
from app.services.cursor_service import SortKey, paginate_scalars
from app.workers.runtime_worker_v1 import RuntimeWorker

router = APIRouter(tags=["plans"])


def expected(value: str | None) -> int:
    raw = (value or "").replace("W/", "").strip('" ')
    if not raw.isdigit():
        raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    return int(raw)


async def owned_plan(db: AsyncSession, plan_id: str, actor: SessionActor, *, lock: bool = False) -> PlanExecution:
    query = select(PlanExecution).where(PlanExecution.id == plan_id)
    if lock:
        query = query.with_for_update()
    plan = await db.scalar(query)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    await _owned_session(db, plan.session_id, actor)
    return plan


@router.get("/sessions/{session_id}/plans")
async def list_session_plans(
    session_id: str, limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    await _owned_session(db, session_id, actor)
    rows, next_cursor = await paginate_scalars(
        db, select(PlanExecution).where(PlanExecution.session_id == session_id),
        sort=(SortKey(PlanExecution.created_at), SortKey(PlanExecution.id)),
        limit=limit, cursor=cursor,
        principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=f"session:{session_id}:plans", filters={},
    )
    return {"items": [await plan_view(db, row) for row in rows], "next_cursor": next_cursor}


@router.post("/sessions/{session_id}/plans", status_code=202)
async def create_plan(
    session_id: str, body: PlanCreateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    session = await _owned_session(db, session_id, actor)
    if session.status != "active":
        raise HTTPException(status_code=409, detail={"code": "session_not_active"})
    record, replay = await _idempotency(
        db, actor=actor, operation="create_plan", key=idempotency_key,
        request={"session_id": session_id, **body.model_dump(mode="json")},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    agent = await db.get(AgentBlueprint, session.entry_agent_id)
    if agent is None or agent.status != "published" or not agent.current_published_version_id:
        raise HTTPException(status_code=409, detail={"code": "owner_agent_unavailable"})
    predecessor = None
    if body.predecessor_plan_id:
        predecessor = await owned_plan(db, body.predecessor_plan_id, actor)
        predecessor_run = await db.get(Run, predecessor.run_id)
        if predecessor_run.lifecycle not in {"completed", "cancelled"}:
            raise HTTPException(status_code=409, detail={"code": "predecessor_plan_not_terminal"})
    message = Message(
        id=uuid7(), session_id=session.id, user_id=session.user_id, scope_type="chat",
        sender_type="user", sender_user_id=session.user_id, recipient_type="session",
        role="user", content=body.goal.strip(), content_format="markdown", status="committed",
    )
    run_budget = default_run_budget("plan")
    run = Run(
        id=uuid7(), session_id=session.id, user_id=session.user_id, mode="plan",
        execution_kind="proposal_generation", lifecycle="running", wait_reason=None,
        outcome=None, entry_agent_id=agent.id, started_at=utcnow(),
        hard_budget_snapshot=run_budget,
        deadline_at=deadline_from_budget(run_budget, mode="plan"),
    )
    plan = PlanExecution(
        id=uuid7(), run_id=run.id, session_id=session.id, user_id=session.user_id,
        owner_agent_id=agent.id, predecessor_plan_id=predecessor.id if predecessor else None,
        goal=body.goal.strip(), deliverable="", definition_status="draft",
        graph_revision=1, version=1,
    )
    work = WorkItem(
        id=uuid7(), run_id=run.id, session_id=session.id, user_id=session.user_id,
        mode="plan", owner_agent_id=agent.id, agent_version_id=agent.current_published_version_id,
        goal=f"Generate a Plan proposal for: {body.goal.strip()}", status="queued",
        execution_epoch=1, attempt_no=1,
    )
    runner = DurableRunner(
        id=uuid7(), work_item_id=work.id, run_id=run.id, session_id=session.id,
        agent_id=agent.id, agent_version_id=agent.current_published_version_id,
        execution_epoch=1, status="queued",
        principal_snapshot={"user_id": session.user_id, "session_id": session.id,
                            "entry_agent_id": agent.id, "current_agent_id": agent.id,
                            "mode": "plan", "delegation_chain": []},
        assembly_snapshot={"agent_version_id": agent.current_published_version_id},
        prompt_manifest={}, input_snapshot={"plan_id": plan.id, "goal": body.goal.strip(),
                                            "proposal_generation": True},
    )
    message.run_id, message.work_item_id = run.id, work.id
    db.add(run)
    await db.flush()
    db.add(plan)
    await db.flush()
    await persist_new_work_runner(db, work, runner)
    db.add(message)
    await db.flush()
    events = ExecutionEventWriter(db)
    message_event = await events.append(
        session_id=session.id, run_id=run.id, event_type="message.created",
        event_dedupe_key=f"command:create_plan:{record.id}:message", actor_type="user",
        actor_id=session.user_id, entity_type="message", entity_id=message.id,
        visibility="user", summary="Plan goal submitted", payload={"message_id": message.id},
        correlation_id=run.id,
    )
    message.caused_by_event_id = message_event.id
    await events.append(
        session_id=session.id, run_id=run.id, event_type="plan.created",
        event_dedupe_key=f"command:create_plan:{record.id}:plan", actor_type="user",
        actor_id=session.user_id, entity_type="plan_execution", entity_id=plan.id,
        entity_version=plan.version, visibility="user", summary="Plan proposal generation started",
        payload={"plan_id": plan.id, "graph_revision": plan.graph_revision}, correlation_id=run.id,
        caused_by_event_id=message_event.id,
    )
    queued = await events.append(
        session_id=session.id, run_id=run.id, event_type="runner.queued",
        event_dedupe_key=f"command:create_plan:{record.id}:runner", actor_type="service",
        actor_id="plan-api", entity_type="runner", entity_id=runner.id,
        entity_version=runner.version, visibility="private", private_owner=("runner", runner.id),
        summary="Plan proposal Runner queued", payload={}, correlation_id=run.id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.runner", action="execute", aggregate_type="runner", aggregate_id=runner.id,
        partition_key=session.id, dedupe_key=f"event:{queued.id}:execute",
        payload_ref={"runner_id": runner.id, "plan_id": plan.id},
    )
    plan_response = await plan_view(db, plan)
    response = {"plan": plan_response, "generation_run": plan_response["run"]}
    record.resource_type, record.resource_id, record.http_status = "plan_execution", plan.id, 202
    record.response_snapshot = response
    await db.commit()
    return response


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: str, db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    return await plan_view(db, await owned_plan(db, plan_id, actor))


@router.get("/plans/{plan_id}/graph")
async def get_plan_graph(
    plan_id: str, db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    plan = await owned_plan(db, plan_id, actor)
    return {"plan_id": plan.id, "graph_revision": plan.graph_revision, **await graph_view(db, plan)}


@router.patch("/plans/{plan_id}")
async def patch_plan(
    plan_id: str, body: PlanPatchRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    record, replay = await _idempotency(
        db, actor=actor, operation="patch_plan", key=idempotency_key,
        request={"plan_id": plan_id, **body.model_dump(mode="json")},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    plan = await owned_plan(db, plan_id, actor, lock=True)
    if plan.version != expected(if_match) or plan.graph_revision != body.expected_graph_revision:
        raise HTTPException(status_code=409, detail={"code": "plan_revision_conflict", "version": plan.version,
                                                    "graph_revision": plan.graph_revision})
    try:
        await replace_draft_graph(
            db, plan, goal=body.goal, deliverable=body.deliverable,
            nodes=body.nodes, dependencies=body.dependencies, increment_revision=True,
        )
    except PlanValidationError as error:
        raise HTTPException(status_code=422, detail={"code": "invalid_plan_graph", "issues": error.issues})
    await ExecutionEventWriter(db).append(
        session_id=plan.session_id, run_id=plan.run_id, event_type="plan.draft_updated",
        event_dedupe_key=f"command:patch_plan:{record.id}", actor_type="user",
        actor_id=plan.user_id, entity_type="plan_execution", entity_id=plan.id,
        entity_version=plan.version, visibility="user", summary="Plan draft updated",
        payload={"graph_revision": plan.graph_revision}, correlation_id=plan.run_id,
    )
    response = await plan_view(db, plan)
    record.resource_type, record.resource_id, record.http_status = "plan_execution", plan.id, 200
    record.response_snapshot = response
    await db.commit()
    return response


@router.post("/plans/{plan_id}/confirm")
async def confirm_plan(
    plan_id: str, body: PlanConfirmRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    record, replay = await _idempotency(
        db, actor=actor, operation="confirm_plan", key=idempotency_key,
        request={"plan_id": plan_id, **body.model_dump(mode="json")},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    plan_view_row = await owned_plan(db, plan_id, actor)
    await db.scalar(select(UserSession).where(UserSession.id == plan_view_row.session_id).with_for_update())
    run = await db.scalar(select(Run).where(Run.id == plan_view_row.run_id).with_for_update())
    plan = await db.scalar(select(PlanExecution).where(PlanExecution.id == plan_id).with_for_update())
    if plan.version != expected(if_match) or plan.graph_revision != body.expected_graph_revision:
        raise HTTPException(status_code=409, detail={"code": "plan_revision_conflict", "version": plan.version,
                                                    "graph_revision": plan.graph_revision})
    if plan.definition_status != "awaiting_confirmation" or run.lifecycle != "waiting" or run.wait_reason != "graph_confirmation":
        raise HTTPException(status_code=409, detail={"code": "plan_not_confirmable"})
    nodes = list(await db.scalars(select(PlanWorkItemNode).where(
        PlanWorkItemNode.plan_execution_id == plan.id
    ).order_by(PlanWorkItemNode.sort_order).with_for_update()))
    deps = list(await db.scalars(select(PlanWorkItemDependency).where(
        PlanWorkItemDependency.plan_execution_id == plan.id
    ).with_for_update()))
    node_inputs = [
        PlanNodeInput(
            node_key=n.node_key, title=n.title, goal=n.goal,
            acceptance_criteria=n.acceptance_criteria, assigned_agent_id=n.assigned_agent_id,
        ) for n in nodes
    ]
    by_id = {node.id: node.node_key for node in nodes}
    dep_inputs = [
        PlanDependencyInput(
            predecessor_node_key=by_id[d.predecessor_node_id], successor_node_key=by_id[d.successor_node_id],
            dependency_type=d.dependency_type,
        ) for d in deps
    ]
    try:
        validated = await validate_graph(db, plan, node_inputs, dep_inputs, resolve_versions=True)
    except PlanValidationError as error:
        raise HTTPException(status_code=422, detail={"code": "invalid_plan_graph", "issues": error.issues})
    if not any(node.assigned_agent_id == plan.owner_agent_id for node in nodes):
        raise HTTPException(status_code=422, detail={"code": "plan_owner_node_required"})
    for agent_id, version_id in validated.agent_versions.items():
        db.add(PlanDeclaredAgent(plan_execution_id=plan.id, agent_id=agent_id, agent_version_id=version_id))
    for node in nodes:
        node.agent_version_id = validated.agent_versions[node.assigned_agent_id]
    await db.flush()
    plan.owner_agent_version_id = validated.agent_versions[plan.owner_agent_id]
    plan.frozen_hash = frozen_hash(plan, nodes, deps, validated.agent_versions)
    plan.definition_status = "frozen"
    plan.confirmed_at = utcnow()
    plan.version += 1
    run.lifecycle = "running"
    run.wait_reason = None
    run.execution_kind = "normal"
    run.version += 1
    incoming = {dependency.successor_node_id for dependency in deps}
    events = ExecutionEventWriter(db)
    confirmed = await events.append(
        session_id=plan.session_id, run_id=run.id, event_type="plan.confirmed",
        event_dedupe_key=f"command:confirm_plan:{record.id}", actor_type="user",
        actor_id=plan.user_id, entity_type="plan_execution", entity_id=plan.id,
        entity_version=plan.version, visibility="user", summary="Plan graph frozen",
        payload={"graph_revision": plan.graph_revision, "frozen_hash": plan.frozen_hash}, correlation_id=run.id,
    )
    for node in nodes:
        if node.id in incoming:
            continue
        if node.assigned_agent_id != plan.owner_agent_id:
            await events.append(
                session_id=plan.session_id, run_id=run.id, event_type="plan.node_ready",
                event_dedupe_key=f"transition:plan_node:{node.id}:ready", actor_type="service",
                actor_id="plan-scheduler", entity_type="plan_work_item_node", entity_id=node.id,
                visibility="user", summary=f"Plan node ready for Owner delegation: {node.title}",
                payload={"node_key": node.node_key, "assigned_agent_id": node.assigned_agent_id},
                correlation_id=run.id, caused_by_event_id=confirmed.id,
            )
            continue
        work = WorkItem(
            id=uuid7(), run_id=run.id, session_id=plan.session_id, user_id=plan.user_id,
            mode="plan", owner_agent_id=plan.owner_agent_id,
            agent_version_id=plan.owner_agent_version_id, plan_node_id=node.id,
            goal=node.goal, status="queued", execution_epoch=1, attempt_no=1,
        )
        runner = DurableRunner(
            id=uuid7(), work_item_id=work.id, run_id=run.id, session_id=plan.session_id,
            agent_id=plan.owner_agent_id, agent_version_id=plan.owner_agent_version_id,
            execution_epoch=1, status="queued",
            principal_snapshot={"user_id": plan.user_id, "session_id": plan.session_id,
                                "entry_agent_id": plan.owner_agent_id, "current_agent_id": plan.owner_agent_id,
                                "mode": "plan", "delegation_chain": []},
            assembly_snapshot={"agent_version_id": plan.owner_agent_version_id}, prompt_manifest={},
            input_snapshot={"plan_id": plan.id, "plan_node_id": node.id, "node_key": node.node_key},
        )
        await persist_new_work_runner(db, work, runner)
        node.work_item_id = work.id
        await db.flush()
        queued = await events.append(
            session_id=plan.session_id, run_id=run.id, event_type="runner.queued",
            event_dedupe_key=f"transition:runner:{runner.id}:0:1:plan_confirm",
            actor_type="service", actor_id="plan-scheduler", entity_type="runner", entity_id=runner.id,
            entity_version=runner.version, visibility="private", private_owner=("runner", runner.id),
            summary="Plan Owner Runner queued", payload={"node_key": node.node_key}, correlation_id=run.id,
            caused_by_event_id=confirmed.id,
        )
        await OutboxWriter(db).enqueue(
            topic="runtime.runner", action="execute", aggregate_type="runner", aggregate_id=runner.id,
            partition_key=plan.session_id, dedupe_key=f"event:{queued.id}:execute",
            payload_ref={"runner_id": runner.id, "plan_id": plan.id, "plan_node_id": node.id},
        )
    await RuntimeWorker("plan-api-scheduler")._schedule_ready_plan_nodes(db, plan, run, confirmed.id)
    response = await plan_view(db, plan)
    record.resource_type, record.resource_id, record.http_status = "plan_execution", plan.id, 200
    record.response_snapshot = response
    await db.commit()
    return response


@router.post("/plans/{plan_id}/revise", status_code=202)
async def revise_plan(
    plan_id: str, body: PlanReviseRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    record, replay = await _idempotency(
        db, actor=actor, operation="revise_plan", key=idempotency_key,
        request={"plan_id": plan_id, **body.model_dump(mode="json")},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    visible = await owned_plan(db, plan_id, actor)
    await db.scalar(select(UserSession).where(UserSession.id == visible.session_id).with_for_update())
    run = await db.scalar(select(Run).where(Run.id == visible.run_id).with_for_update())
    plan = await db.scalar(select(PlanExecution).where(PlanExecution.id == plan_id).with_for_update())
    if plan.version != expected(if_match) or plan.graph_revision != body.expected_graph_revision:
        raise HTTPException(status_code=409, detail={"code": "plan_revision_conflict", "version": plan.version,
                                                    "graph_revision": plan.graph_revision})
    if plan.definition_status != "awaiting_confirmation" or run.lifecycle != "waiting" or run.wait_reason != "graph_confirmation":
        raise HTTPException(status_code=409, detail={"code": "plan_not_revisable"})
    agent = await db.get(AgentBlueprint, plan.owner_agent_id)
    if agent is None or agent.status != "published" or not agent.current_published_version_id:
        raise HTTPException(status_code=409, detail={"code": "owner_agent_unavailable"})
    message = Message(
        id=uuid7(), session_id=plan.session_id, user_id=plan.user_id, scope_type="chat",
        sender_type="user", sender_user_id=plan.user_id, recipient_type="session",
        role="user", content=body.instruction.strip(), content_format="markdown", status="committed",
        run_id=run.id,
    )
    work = WorkItem(
        id=uuid7(), run_id=run.id, session_id=plan.session_id, user_id=plan.user_id,
        mode="plan", owner_agent_id=plan.owner_agent_id,
        agent_version_id=agent.current_published_version_id,
        goal=f"Revise Plan proposal: {body.instruction.strip()}", status="queued",
        execution_epoch=1, attempt_no=1,
    )
    runner = DurableRunner(
        id=uuid7(), work_item_id=work.id, run_id=run.id, session_id=plan.session_id,
        agent_id=plan.owner_agent_id, agent_version_id=agent.current_published_version_id,
        execution_epoch=1, status="queued",
        principal_snapshot={"user_id": plan.user_id, "session_id": plan.session_id,
                            "entry_agent_id": plan.owner_agent_id, "current_agent_id": plan.owner_agent_id,
                            "mode": "plan", "delegation_chain": []},
        assembly_snapshot={"agent_version_id": agent.current_published_version_id}, prompt_manifest={},
        input_snapshot={"plan_id": plan.id, "instruction": body.instruction.strip(),
                        "proposal_generation": True, "increment_graph_revision": True},
    )
    message.work_item_id = work.id
    plan.definition_status = "draft"
    plan.version += 1
    run.lifecycle = "running"
    run.wait_reason = None
    run.execution_kind = "proposal_generation"
    run.version += 1
    await persist_new_work_runner(db, work, runner)
    db.add(message)
    await db.flush()
    events = ExecutionEventWriter(db)
    message_event = await events.append(
        session_id=plan.session_id, run_id=run.id, event_type="message.created",
        event_dedupe_key=f"command:revise_plan:{record.id}:message", actor_type="user",
        actor_id=plan.user_id, entity_type="message", entity_id=message.id, visibility="user",
        summary="Plan revision requested", payload={"message_id": message.id}, correlation_id=run.id,
    )
    message.caused_by_event_id = message_event.id
    started = await events.append(
        session_id=plan.session_id, run_id=run.id, event_type="plan.revision_started",
        event_dedupe_key=f"command:revise_plan:{record.id}:start", actor_type="user",
        actor_id=plan.user_id, entity_type="plan_execution", entity_id=plan.id,
        entity_version=plan.version, visibility="user", summary="Plan revision generation started",
        payload={"current_graph_revision": plan.graph_revision}, correlation_id=run.id,
        caused_by_event_id=message_event.id,
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.runner", action="execute", aggregate_type="runner", aggregate_id=runner.id,
        partition_key=plan.session_id, dedupe_key=f"event:{started.id}:execute",
        payload_ref={"runner_id": runner.id, "plan_id": plan.id},
    )
    response = await plan_view(db, plan)
    record.resource_type, record.resource_id, record.http_status = "plan_execution", plan.id, 202
    record.response_snapshot = response
    await db.commit()
    return response


@router.post("/plans/{plan_id}/cancel")
async def cancel_plan(
    plan_id: str, body: RunCancelRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    record, replay = await _idempotency(
        db, actor=actor, operation="cancel_plan", key=idempotency_key,
        request={"plan_id": plan_id, **body.model_dump(mode="json")},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    visible = await owned_plan(db, plan_id, actor)
    await db.scalar(select(UserSession).where(UserSession.id == visible.session_id).with_for_update())
    run = await db.scalar(select(Run).where(Run.id == visible.run_id).with_for_update())
    plan = await db.scalar(select(PlanExecution).where(PlanExecution.id == plan_id).with_for_update())
    if plan.version != expected(if_match):
        raise HTTPException(status_code=409, detail={"code": "plan_revision_conflict", "version": plan.version,
                                                    "graph_revision": plan.graph_revision})
    if run.lifecycle in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail={"code": "run_terminal", "lifecycle": run.lifecycle})
    now = utcnow()
    run.cancellation_requested_at = now
    work_items = list(await db.scalars(select(WorkItem).where(WorkItem.run_id == run.id).with_for_update()))
    non_terminal_work = {"queued", "running", "waiting_user", "waiting_agent", "waiting_tool", "waiting_resource"}
    for work in work_items:
        if work.status not in non_terminal_work:
            continue
        runners = list(await db.scalars(select(DurableRunner).where(
            DurableRunner.work_item_id == work.id,
            DurableRunner.status.in_(("queued", "running")),
        ).with_for_update()))
        for runner in runners:
            runner.status = "cancelled"
            runner.lease_expires_at = None
            runner.version += 1
        invocations = list(await db.scalars(select(ToolInvocation).where(
            ToolInvocation.work_item_id == work.id,
            ToolInvocation.status.in_(("created", "waiting_confirmation", "waiting_resource", "queued", "running", "cancel_requested")),
        ).with_for_update()))
        for invocation in invocations:
            invocation.status = "cancelled"
            invocation.error_code = "run_cancelled"
            invocation.version += 1
            confirmation = await db.scalar(select(ToolConfirmation).where(
                ToolConfirmation.tool_invocation_id == invocation.id,
                ToolConfirmation.status == "pending",
            ).with_for_update())
            if confirmation:
                confirmation.status = "cancelled"
                confirmation.decided_at = now
                confirmation.version += 1
        agent_invocations = list(await db.scalars(select(AgentInvocation).where(
            (AgentInvocation.caller_work_item_id == work.id) | (AgentInvocation.target_work_item_id == work.id),
            AgentInvocation.status.in_(("created", "queued", "running")),
        ).with_for_update()))
        for agent_invocation in agent_invocations:
            agent_invocation.status = "cancelled"
            agent_invocation.error_code = "run_cancelled"
            agent_invocation.version += 1
        work.status = "cancelled"
        work.active_runner_id = None
        work.error_code = "run_cancelled"
        work.version += 1
    if plan.definition_status != "frozen":
        plan.definition_status = "cancelled"
        plan.version += 1
    run.lifecycle = "cancelled"
    run.wait_reason = None
    run.outcome = None
    run.error_code = "cancelled_by_user"
    run.result_summary = body.reason
    run.finished_at = now
    run.version += 1
    events = ExecutionEventWriter(db)
    cancelled = await events.append(
        session_id=plan.session_id, run_id=run.id, event_type="plan.cancelled",
        event_dedupe_key=f"command:cancel_plan:{record.id}", actor_type="user",
        actor_id=plan.user_id, entity_type="plan_execution", entity_id=plan.id,
        entity_version=plan.version, visibility="user", summary="Plan execution cancelled",
        payload={"reason": body.reason, "definition_status": plan.definition_status}, correlation_id=run.id,
    )
    run_cancelled = await events.append(
        session_id=plan.session_id, run_id=run.id, event_type="run.cancelled",
        event_dedupe_key=f"command:cancel_plan:{record.id}:run", actor_type="user",
        actor_id=plan.user_id, entity_type="run", entity_id=run.id,
        entity_version=run.version, visibility="user", summary="Run cancelled",
        payload={"reason": body.reason}, correlation_id=run.id, caused_by_event_id=cancelled.id,
    )
    await create_notification_delivery_if_channel(
        db, session_id=plan.session_id, triggering_event_id=run_cancelled.id,
        event_type="run_cancelled", run_id=run.id,
    )
    response = await plan_view(db, plan)
    record.resource_type, record.resource_id, record.http_status = "plan_execution", plan.id, 200
    record.response_snapshot = response
    await db.commit()
    return response


@router.get("/plans/{plan_id}/work-items")
async def list_plan_work_items(
    plan_id: str, limit: int = 100, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    plan = await owned_plan(db, plan_id, actor)
    rows, next_cursor = await paginate_scalars(db, select(WorkItem).where(
        WorkItem.run_id == plan.run_id,
        WorkItem.mode == "plan",
        WorkItem.plan_node_id.is_not(None),
    ), sort=(SortKey(WorkItem.created_at, descending=False), SortKey(WorkItem.id, descending=False)),
        limit=limit, cursor=cursor,
        principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=f"plan:{plan_id}:work-items", filters={})
    return {"items": [{"id": row.id, "plan_node_id": row.plan_node_id, "status": row.status,
                       "owner_agent_id": row.owner_agent_id, "result_summary": row.result_summary,
                       "error_code": row.error_code, "version": row.version} for row in rows],
            "next_cursor": next_cursor}
