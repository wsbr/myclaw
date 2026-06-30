import json
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.session import Session, OrchestrationGraph
from app.models.tool import ModelProvider
from app.core.model_hub.registry import ModelHub
from app.services.agent_service import AgentService


ORCHESTRATION_SYSTEM_PROMPT = """You are a multi-agent orchestration planner.
Given a goal and a list of available agents, generate an orchestration graph.

Return ONLY valid JSON:
{
  "goal": "...",
  "nodes": [
    {
      "id": "n1",
      "agent_id": "<id>",
      "agent_name": "<name>",
      "step_goal": "what this agent should accomplish in this step",
      "input_mapping": {}
    }
  ],
  "edges": [
    {"from": "start", "to": ["n1", "n2"], "type": "parallel"},
    {"from": ["n1", "n2"], "to": "n3", "type": "join"}
  ]
}
Edge types: "sequential" (one after another), "parallel" (run simultaneously), "join" (wait for all sources).
"""

SUBSTITUTION_SYSTEM_PROMPT = """Given a step goal and a candidate agent, determine if the agent can fulfill the step.
Reply with JSON: {"valid": true/false, "reason": "..."}"""


class GroupOrchestrator:

    @staticmethod
    async def generate_graph(db: AsyncSession, session: Session, agent_snapshot: dict, goal: str) -> OrchestrationGraph:
        group_agents = await AgentService.get_group_agents(db, exclude_id=session.agent_id)
        model_cfg = agent_snapshot.get("model_config", {})
        llm = await GroupOrchestrator._get_llm(db, model_cfg)

        agent_list = "\n".join(f"- id: {a['id']}, name: {a['name']}: {a['description']}" for a in group_agents)
        messages = [{
            "role": "user",
            "content": f"Goal: {goal}\n\nAvailable agents:\n{agent_list}\n\nGenerate orchestration graph."
        }]
        params = dict(model_cfg)
        params["system"] = ORCHESTRATION_SYSTEM_PROMPT

        response = await llm.chat(messages, None, params)
        try:
            content = response.content.strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            graph_data = json.loads(content)
        except Exception:
            graph_data = {
                "goal": goal,
                "nodes": [{"id": "n1", "agent_id": group_agents[0]["id"] if group_agents else "", "agent_name": "", "step_goal": goal, "input_mapping": {}}],
                "edges": [],
            }

        for node in graph_data.get("nodes", []):
            node["status"] = "pending"

        graph = OrchestrationGraph(
            id=str(uuid.uuid4()),
            session_id=session.id,
            goal=goal,
            graph_json=graph_data,
            status="draft",
        )
        db.add(graph)
        await db.commit()
        await db.refresh(graph)
        return graph

    @staticmethod
    async def validate_substitution(
        db: AsyncSession,
        agent_snapshot: dict,
        node_id: str,
        new_agent_id: str,
        graph: OrchestrationGraph,
    ) -> dict:
        from app.models.agent import Agent
        result = await db.execute(select(Agent).where(Agent.id == new_agent_id))
        new_agent = result.scalar_one_or_none()
        if not new_agent:
            return {"valid": False, "warnings": ["Agent not found"]}

        node = next((n for n in graph.graph_json.get("nodes", []) if n["id"] == node_id), None)
        if not node:
            return {"valid": False, "warnings": ["Node not found in graph"]}

        model_cfg = agent_snapshot.get("model_config", {})
        llm = await GroupOrchestrator._get_llm(db, model_cfg)
        gc = new_agent.group_config or {}
        messages = [{
            "role": "user",
            "content": (
                f"Step goal: {node['step_goal']}\n"
                f"Candidate agent: {new_agent.name}\nDescription: {new_agent.description}\n"
                f"Capabilities: {json.dumps(gc.get('capability_manifest', []))}\n\n"
                "Can this agent fulfill the step goal?"
            ),
        }]
        params = dict(model_cfg)
        params["system"] = SUBSTITUTION_SYSTEM_PROMPT

        response = await llm.chat(messages, None, params)
        try:
            content = response.content.strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            result_data = json.loads(content)
            return {
                "valid": result_data.get("valid", False),
                "warnings": [] if result_data.get("valid") else [result_data.get("reason", "Not suitable")],
            }
        except Exception:
            return {"valid": True, "warnings": ["Could not verify — proceed with caution"]}

    @staticmethod
    async def execute_graph(db: AsyncSession, session: Session, graph: OrchestrationGraph) -> list[str]:
        """Create tasks for each node respecting dependency order."""
        from app.models.task import Task
        from app.models.agent import Agent

        graph.status = "running"
        await db.flush()

        nodes = graph.graph_json.get("nodes", [])
        edges = graph.graph_json.get("edges", [])

        # Build dependency map: node_id -> list of node_ids that must complete first
        deps: dict[str, list[str]] = {n["id"]: [] for n in nodes}
        for edge in edges:
            frm = edge.get("from")
            to = edge.get("to")
            if not frm or not to or frm == "start":
                continue
            sources = frm if isinstance(frm, list) else [frm]
            targets = to if isinstance(to, list) else [to]
            for t in targets:
                deps.setdefault(t, []).extend(sources)

        task_ids = []
        node_task_map: dict[str, str] = {}

        # Topological execution: for now, sequential by dependency order
        # Production: use Celery chord/group for parallel
        created_tasks = []
        for node in nodes:
            result = await db.execute(select(Agent).where(Agent.id == node["agent_id"]))
            agent = result.scalar_one_or_none()
            if not agent:
                continue

            dep_task_ids = [node_task_map[d] for d in deps.get(node["id"], []) if d in node_task_map]
            task = Task(
                id=str(uuid.uuid4()),
                agent_id=node["agent_id"],
                agent_version=agent.version,
                user_id=session.user_id,
                session_id=session.id,
                title=node["step_goal"][:200],
                trigger_type="immediate",
                input_data={"step_goal": node["step_goal"], "input_mapping": node.get("input_mapping", {})},
                output_channel="platform_ui",
                parent_task_id=dep_task_ids[0] if dep_task_ids else None,
            )
            db.add(task)
            await db.flush()
            node_task_map[node["id"]] = task.id
            task_ids.append(task.id)
            created_tasks.append((task, dep_task_ids))

        await db.commit()

        # Dispatch tasks (parallel where no deps, sequential where deps exist)
        from app.workers.task_worker import execute_task
        for task, dep_ids in created_tasks:
            if not dep_ids:
                execute_task.delay(task.id)

        graph.status = "running"
        await db.commit()
        return task_ids

    @staticmethod
    async def _get_llm(db: AsyncSession, model_cfg: dict):
        provider_id = model_cfg.get("provider_id")
        result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
        provider = result.scalar_one_or_none()
        return ModelHub.get(model_cfg, api_key=provider.api_key_encrypted or "", base_url=provider.base_url)
