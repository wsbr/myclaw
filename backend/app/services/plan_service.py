"""Plan graph validation, draft replacement, and immutable freeze helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint, AgentVersionBinding
from app.models.foundation import PlatformAccount
from app.models.plan_v1 import PlanDeclaredAgent, PlanExecution, PlanWorkItemDependency, PlanWorkItemNode
from app.models.runtime_v1 import Run
from app.schemas.plan_v1 import PlanDependencyInput, PlanNodeInput
from app.schemas.runtime_v1 import RunResponse


class PlanValidationError(ValueError):
    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("; ".join(issues))


@dataclass(frozen=True)
class ValidatedGraph:
    nodes: tuple[PlanNodeInput, ...]
    dependencies: tuple[PlanDependencyInput, ...]
    agent_versions: dict[str, str]


async def validate_graph(
    db: AsyncSession, plan: PlanExecution, nodes: list[PlanNodeInput],
    dependencies: list[PlanDependencyInput], *, resolve_versions: bool = False,
) -> ValidatedGraph:
    issues: list[str] = []
    keys = [node.node_key for node in nodes]
    if len(set(keys)) != len(keys):
        issues.append("node_key values must be unique")
    key_set = set(keys)
    edges: set[tuple[str, str]] = set()
    successors = {key: [] for key in keys}
    indegree = {key: 0 for key in keys}
    for dependency in dependencies:
        edge = (dependency.predecessor_node_key, dependency.successor_node_key)
        if dependency.dependency_type not in {"success", "settled"}:
            issues.append(f"invalid dependency_type for {edge}")
            continue
        if edge[0] not in key_set or edge[1] not in key_set:
            issues.append(f"dependency references an unknown node: {edge}")
            continue
        if edge[0] == edge[1]:
            issues.append(f"self dependency is not allowed: {edge[0]}")
            continue
        if edge in edges:
            issues.append(f"duplicate dependency: {edge}")
            continue
        edges.add(edge)
        successors[edge[0]].append(edge[1])
        indegree[edge[1]] += 1
    queue = [key for key, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        key = queue.pop()
        visited += 1
        for successor in successors[key]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)
    if keys and visited != len(keys):
        issues.append("Plan graph must be acyclic")

    owner = await db.get(AgentBlueprint, plan.owner_agent_id)
    owner_version_id = owner.current_published_version_id if owner else None
    allowed = {plan.owner_agent_id}
    if owner_version_id:
        allowed.update(await db.scalars(select(AgentVersionBinding.bound_agent_id).where(
            AgentVersionBinding.agent_version_id == owner_version_id
        )))
    account = await db.scalar(select(PlatformAccount).where(PlatformAccount.user_id == plan.user_id))
    agent_versions: dict[str, str] = {}
    for agent_id in {node.assigned_agent_id for node in nodes} | {plan.owner_agent_id}:
        if agent_id not in allowed:
            issues.append(f"Agent is outside the Owner direct binding scope: {agent_id}")
            continue
        agent = await db.get(AgentBlueprint, agent_id)
        if agent is None or agent.status != "published" or not agent.current_published_version_id:
            issues.append(f"Agent is unavailable: {agent_id}")
            continue
        if agent.visibility == "private" and (account is None or agent.owner_account_id != account.id):
            issues.append(f"Agent is not accessible to this user: {agent_id}")
            continue
        if resolve_versions:
            version = await db.get(AgentAssemblyVersion, agent.current_published_version_id)
            if version is None or version.immutable_at is None:
                issues.append(f"Agent has no immutable published version: {agent_id}")
            else:
                agent_versions[agent_id] = version.id
    if issues:
        raise PlanValidationError(issues)
    return ValidatedGraph(tuple(nodes), tuple(dependencies), agent_versions)


async def replace_draft_graph(
    db: AsyncSession, plan: PlanExecution, *, goal: str, deliverable: str,
    nodes: list[PlanNodeInput], dependencies: list[PlanDependencyInput],
    increment_revision: bool,
) -> None:
    await validate_graph(db, plan, nodes, dependencies)
    if plan.definition_status == "frozen":
        raise PlanValidationError(["frozen Plan definitions are immutable"])
    await db.execute(delete(PlanWorkItemDependency).where(
        PlanWorkItemDependency.plan_execution_id == plan.id
    ))
    await db.execute(delete(PlanWorkItemNode).where(
        PlanWorkItemNode.plan_execution_id == plan.id
    ))
    if increment_revision:
        plan.graph_revision += 1
    plan.version += 1
    plan.goal = goal.strip()
    plan.deliverable = deliverable.strip()
    plan.definition_status = "awaiting_confirmation"
    by_key: dict[str, PlanWorkItemNode] = {}
    for order, value in enumerate(nodes):
        node = PlanWorkItemNode(
            plan_execution_id=plan.id, node_key=value.node_key, title=value.title.strip(),
            goal=value.goal.strip(), acceptance_criteria=value.acceptance_criteria.strip(),
            assigned_agent_id=value.assigned_agent_id, sort_order=order,
            definition_version=plan.graph_revision,
        )
        db.add(node)
        by_key[value.node_key] = node
    await db.flush()
    for value in dependencies:
        db.add(PlanWorkItemDependency(
            plan_execution_id=plan.id,
            predecessor_node_id=by_key[value.predecessor_node_key].id,
            successor_node_id=by_key[value.successor_node_key].id,
            dependency_type=value.dependency_type,
        ))
    await db.flush()


async def graph_view(db: AsyncSession, plan: PlanExecution) -> dict:
    nodes = list(await db.scalars(select(PlanWorkItemNode).where(
        PlanWorkItemNode.plan_execution_id == plan.id
    ).order_by(PlanWorkItemNode.sort_order)))
    dependencies = list(await db.scalars(select(PlanWorkItemDependency).where(
        PlanWorkItemDependency.plan_execution_id == plan.id
    )))
    by_id = {node.id: node.node_key for node in nodes}
    return {
        "nodes": [{
            "id": node.id, "node_key": node.node_key, "title": node.title,
            "goal": node.goal, "acceptance_criteria": node.acceptance_criteria,
            "assigned_agent_id": node.assigned_agent_id,
            "agent_version_id": node.agent_version_id, "work_item_id": node.work_item_id,
            "sort_order": node.sort_order, "definition_version": node.definition_version,
        } for node in nodes],
        "dependencies": [{
            "predecessor_node_key": by_id[row.predecessor_node_id],
            "successor_node_key": by_id[row.successor_node_id],
            "dependency_type": row.dependency_type,
        } for row in dependencies],
    }


async def plan_view(db: AsyncSession, plan: PlanExecution) -> dict:
    run = await db.get(Run, plan.run_id)
    graph = await graph_view(db, plan)
    declared = list(await db.scalars(select(PlanDeclaredAgent).where(
        PlanDeclaredAgent.plan_execution_id == plan.id
    )))
    return {
        "id": plan.id, "run_id": plan.run_id, "session_id": plan.session_id,
        "owner_agent_id": plan.owner_agent_id, "owner_agent_version_id": plan.owner_agent_version_id,
        "predecessor_plan_id": plan.predecessor_plan_id, "goal": plan.goal,
        "deliverable": plan.deliverable, "definition_status": plan.definition_status,
        "graph_revision": plan.graph_revision,
        "confirmed_at": plan.confirmed_at.isoformat() if plan.confirmed_at else None,
        "frozen_hash": plan.frozen_hash, "version": plan.version,
        "declared_agents": [{"agent_id": row.agent_id, "agent_version_id": row.agent_version_id} for row in declared],
        "run": RunResponse.model_validate(run).model_dump(mode="json"), **graph,
    }


def frozen_hash(plan: PlanExecution, nodes: list[PlanWorkItemNode], dependencies: list[PlanWorkItemDependency], versions: dict[str, str]) -> str:
    payload = {
        "plan_id": plan.id, "graph_revision": plan.graph_revision, "goal": plan.goal,
        "deliverable": plan.deliverable,
        "nodes": [{"key": n.node_key, "title": n.title, "goal": n.goal,
                   "acceptance": n.acceptance_criteria, "agent": n.assigned_agent_id,
                   "version": versions[n.assigned_agent_id]} for n in nodes],
        "dependencies": sorted((d.predecessor_node_id, d.successor_node_id, d.dependency_type) for d in dependencies),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
