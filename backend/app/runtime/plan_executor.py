import json
import uuid
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.session import Session, Runner
from app.models.tool import ModelProvider
from app.core.model_hub.registry import ModelHub
from app.runtime.tracer import Tracer


PLAN_SYSTEM_PROMPT = """You are a planning assistant. Given a goal, generate a structured execution plan.
Return ONLY valid JSON with this exact structure:
{
  "goal": "...",
  "steps": [
    {"id": "s1", "description": "...", "tool": null, "depends_on": []},
    {"id": "s2", "description": "...", "tool": "web_search", "depends_on": ["s1"]}
  ]
}
Each step has: id (s1,s2...), description, optional tool name, depends_on (list of step ids).
"""


class PlanExecutor:

    @staticmethod
    async def generate_plan(db: AsyncSession, session: Session, agent_snapshot: dict, goal: str) -> dict:
        model_cfg = agent_snapshot.get("model_config", {})
        llm = await PlanExecutor._get_llm(db, model_cfg)

        messages = [{"role": "user", "content": f"Goal: {goal}\n\nGenerate a structured execution plan."}]
        params = dict(model_cfg)
        params["system"] = PLAN_SYSTEM_PROMPT

        response = await llm.chat(messages, None, params)
        try:
            # Extract JSON from response
            content = response.content.strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            plan = json.loads(content)
        except Exception:
            plan = {
                "goal": goal,
                "steps": [{"id": "s1", "description": goal, "tool": None, "depends_on": []}],
            }

        plan["plan_id"] = str(uuid.uuid4())
        plan["session_id"] = session.id
        plan["status"] = "draft"
        for step in plan.get("steps", []):
            step["status"] = "pending"

        return plan

    @staticmethod
    async def execute_plan(db: AsyncSession, session: Session, agent_snapshot: dict, plan: dict) -> str:
        """Execute plan steps in order, returning task_id."""
        from app.models.task import Task
        from app.models.agent import Agent

        result = await db.execute(select(Agent).where(Agent.id == session.agent_id))
        agent = result.scalar_one_or_none()

        task = Task(
            id=str(uuid.uuid4()),
            agent_id=session.agent_id,
            agent_version=agent.version,
            user_id=session.user_id,
            session_id=session.id,
            title=plan.get("goal", "Plan execution"),
            trigger_type="immediate",
            input_data=plan,
            output_channel="platform_ui",
        )
        db.add(task)
        await db.commit()

        from app.workers.task_worker import execute_plan_task
        celery_res = execute_plan_task.delay(task.id, plan)
        task.celery_task_id = celery_res.id
        await db.commit()
        return task.id

    @staticmethod
    async def _get_llm(db: AsyncSession, model_cfg: dict):
        provider_id = model_cfg.get("provider_id")
        result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
        provider = result.scalar_one_or_none()
        return ModelHub.get(model_cfg, api_key=provider.api_key_encrypted or "", base_url=provider.base_url)
