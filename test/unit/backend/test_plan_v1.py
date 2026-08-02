"""Relationship-backed Plan graph validation and freeze tests."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint, AgentVersionBinding
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    ExecutionEvent, ExecutionEventRecipient, OutboxCommand, PlatformAccount, UserIdentity, UserSession,
)
from app.models.plan_v1 import PlanExecution, PlanWorkItemDependency, PlanWorkItemNode
from app.models.registry import RegisteredModel
from app.models.runtime_v1 import DurableRunner, Run, WorkItem
from app.schemas.plan_v1 import PlanDependencyInput, PlanNodeInput
from app.services.plan_service import PlanValidationError, replace_draft_graph, validate_graph
from app.workers.runtime_worker_v1 import RuntimeWorker

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_plan_graph_is_a_dag_with_fixed_direct_agent_versions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, RegisteredModel.__table__,
        AgentBlueprint.__table__, AgentAssemblyVersion.__table__, AgentVersionBinding.__table__,
        UserSession.__table__, ExecutionEvent.__table__, ExecutionEventRecipient.__table__,
        Run.__table__, WorkItem.__table__, DurableRunner.__table__, PlanExecution.__table__,
        PlanWorkItemNode.__table__, PlanWorkItemDependency.__table__,
        OutboxCommand.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, model_id = uuid7(), uuid7()
    owner_id, owner_version_id = uuid7(), uuid7()
    child_id, child_version_id = uuid7(), uuid7()
    session_id, run_id, plan_id = uuid7(), uuid7(), uuid7()
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="u-plan", status="active"),
            PlatformAccount(id=account_id, user_id="u-plan", display_name="Plan User", status="active"),
            RegisteredModel(id=model_id, key="mock-plan", provider="openai_compatible", model_name="mock",
                            default_parameters={}, capabilities={}, enabled=True, config_version=1),
        ])
        await db.flush()
        db.add_all([
            AgentBlueprint(id=owner_id, owner_account_id=account_id, visibility="enterprise", status="published",
                           current_published_version_id=owner_version_id, draft_version_id=uuid7(), next_version_no=2),
            AgentBlueprint(id=child_id, owner_account_id=account_id, visibility="enterprise", status="published",
                           current_published_version_id=child_version_id, draft_version_id=uuid7(), next_version_no=2),
        ])
        await db.flush()
        db.add_all([
            AgentAssemblyVersion(id=owner_version_id, agent_id=owner_id, version_no=1,
                                 publication_status="published", name="Owner", description="D", instructions="I",
                                 model_id=model_id, group_enabled=False, created_by_account_id=account_id,
                                 immutable_at=utcnow(), published_at=utcnow(), content_hash="a" * 64),
            AgentAssemblyVersion(id=child_version_id, agent_id=child_id, version_no=1,
                                 publication_status="published", name="Child", description="D", instructions="I",
                                 model_id=model_id, group_enabled=False, created_by_account_id=account_id,
                                 immutable_at=utcnow(), published_at=utcnow(), content_hash="b" * 64),
        ])
        await db.flush()
        db.add(AgentVersionBinding(agent_version_id=owner_version_id, bound_agent_id=child_id, sort_order=0))
        db.add(UserSession(id=session_id, user_id="u-plan", entry_agent_id=owner_id,
                           status="active", origin_type="web"))
        db.add(Run(id=run_id, session_id=session_id, user_id="u-plan", mode="plan",
                   execution_kind="proposal_generation", lifecycle="waiting", wait_reason="graph_confirmation",
                   entry_agent_id=owner_id, started_at=utcnow()))
        plan = PlanExecution(id=plan_id, run_id=run_id, session_id=session_id, user_id="u-plan",
                             owner_agent_id=owner_id, goal="Ship", deliverable="", definition_status="draft",
                             graph_revision=1, version=1)
        db.add(plan)
        await db.flush()
        nodes = [
            PlanNodeInput(node_key="research", title="Research", goal="Find facts",
                          acceptance_criteria="Sources captured", assigned_agent_id=child_id),
            PlanNodeInput(node_key="deliver", title="Deliver", goal="Write result",
                          acceptance_criteria="User-ready response", assigned_agent_id=owner_id),
        ]
        dependencies = [PlanDependencyInput(predecessor_node_key="research",
                                            successor_node_key="deliver", dependency_type="success")]
        await replace_draft_graph(db, plan, goal="Ship", deliverable="Report", nodes=nodes,
                                  dependencies=dependencies, increment_revision=False)
        assert plan.definition_status == "awaiting_confirmation"
        assert plan.graph_revision == 1
        assert plan.version == 2
        validated = await validate_graph(db, plan, nodes, dependencies, resolve_versions=True)
        assert validated.agent_versions == {owner_id: owner_version_id, child_id: child_version_id}

    async with factory.begin() as db:
        plan = await db.get(PlanExecution, plan_id)
        cycle = [
            PlanDependencyInput(predecessor_node_key="research", successor_node_key="deliver"),
            PlanDependencyInput(predecessor_node_key="deliver", successor_node_key="research"),
        ]
        with pytest.raises(PlanValidationError, match="acyclic"):
            await replace_draft_graph(db, plan, goal="Ship", deliverable="Report", nodes=nodes,
                                      dependencies=cycle, increment_revision=True)
        stored = list(await db.scalars(select(PlanWorkItemNode).where(
            PlanWorkItemNode.plan_execution_id == plan_id
        )))
        assert {node.node_key for node in stored} == {"research", "deliver"}

    async with factory.begin() as db:
        plan = await db.get(PlanExecution, plan_id)
        plan.definition_status = "frozen"
        with pytest.raises(PlanValidationError, match="immutable"):
            await replace_draft_graph(db, plan, goal="Changed", deliverable="Changed", nodes=nodes,
                                      dependencies=[], increment_revision=True)

    async with factory.begin() as db:
        plan = await db.get(PlanExecution, plan_id)
        plan.owner_agent_version_id = owner_version_id
        run = await db.get(Run, run_id)
        run.lifecycle = "running"
        run.wait_reason = None
        run.execution_kind = "normal"
        stored_nodes = list(await db.scalars(select(PlanWorkItemNode).where(
            PlanWorkItemNode.plan_execution_id == plan_id
        ).order_by(PlanWorkItemNode.sort_order)))
        for node in stored_nodes:
            node.agent_version_id = child_version_id if node.assigned_agent_id == child_id else owner_version_id
        await RuntimeWorker("plan-test")._schedule_ready_plan_nodes(db, plan, run, "cause-1")

    async with factory.begin() as db:
        run = await db.get(Run, run_id)
        plan = await db.get(PlanExecution, plan_id)
        stored_nodes = list(await db.scalars(select(PlanWorkItemNode).where(
            PlanWorkItemNode.plan_execution_id == plan_id
        ).order_by(PlanWorkItemNode.sort_order)))
        coordination_runner = next(row for row in await db.scalars(select(DurableRunner))
                                   if row.input_snapshot.get("plan_coordination_for_node_id"))
        coordination_work = await db.get(WorkItem, coordination_runner.work_item_id)
        coordination_runner.status = "running"
        coordination_work.status = "running"
        await RuntimeWorker("plan-test")._settle_plan_work_failure(
            db, coordination_runner, coordination_work, run, RuntimeError("child dispatch unavailable"),
        )
        await db.flush()
        node_works = [await db.get(WorkItem, node.work_item_id) for node in stored_nodes]
        assert [work.status for work in node_works] == ["blocked", "blocked"]
        summary_runners = [row for row in await db.scalars(select(DurableRunner))
                           if row.input_snapshot.get("plan_settlement")]
        assert len(summary_runners) == 1
        assert plan.definition_status == "frozen"
        assert run.lifecycle == "settling"
    await engine.dispose()
