import json
import uuid
from datetime import datetime
from collections import deque

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.session import Session, Runner, ExecutionPlan
from app.models.tool import ModelProvider
from app.core.model_hub.registry import ModelHub
from app.runtime.tracer import Tracer
from app.schemas.plan import PlanPreview, PlanStepSchema


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
Break complex goals into 2-6 concrete, sequential steps when appropriate.
"""

PLAN_REVISE_PROMPT = """You are a planning assistant. The user wants to modify an existing execution plan.
Return ONLY valid JSON with the same structure as the current plan (goal + steps array).
Apply the user's instruction while preserving step ids where possible.
Each step must include: id, description, optional tool, depends_on, status="pending".
"""


def parse_llm_json(content: str) -> dict:
    text = content.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    return json.loads(text)


def normalize_steps(steps: list[dict]) -> list[dict]:
    normalized = []
    for i, step in enumerate(steps):
        normalized.append({
            "id": step.get("id") or f"s{i + 1}",
            "description": step.get("description", ""),
            "tool": step.get("tool"),
            "depends_on": step.get("depends_on") or [],
            "status": step.get("status", "pending"),
            "output": step.get("output"),
        })
    return normalized


def sort_plan_steps(steps: list[dict]) -> list[dict]:
    """Topological sort by depends_on; fall back to original order on cycles."""
    by_id = {step["id"]: step for step in steps}
    indegree = {step["id"]: 0 for step in steps}
    adjacency: dict[str, list[str]] = {step["id"]: [] for step in steps}

    for step in steps:
        for dep in step.get("depends_on") or []:
            if dep not in by_id:
                continue
            indegree[step["id"]] += 1
            adjacency[dep].append(step["id"])

    queue = deque([step_id for step_id, deg in indegree.items() if deg == 0])
    ordered_ids: list[str] = []

    while queue:
        current = queue.popleft()
        ordered_ids.append(current)
        for nxt in adjacency[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if len(ordered_ids) != len(steps):
        return steps
    return [by_id[step_id] for step_id in ordered_ids]


def build_step_prompt(step: dict, step_outputs: dict[str, str], goal: str) -> str:
    parts = [
        f"Overall goal: {goal}",
        f"Current step ({step['id']}): {step['description']}",
    ]
    for dep_id in step.get("depends_on") or []:
        if dep_id in step_outputs:
            parts.append(f"Output from step {dep_id}:\n{step_outputs[dep_id]}")
    return "\n\n".join(parts)


async def broadcast_session_event(session_id: str, event_type: str, data: dict) -> None:
    try:
        import redis.asyncio as aioredis
        from app.config import settings

        payload = json.dumps({"type": event_type, "data": data})
        async with aioredis.from_url(settings.redis_url, decode_responses=True) as r:
            await r.publish(f"myclaw:session:{session_id}", payload)
    except Exception:
        pass


def plan_to_preview(plan: ExecutionPlan) -> PlanPreview:
    plan_json = plan.plan_json or {}
    steps = normalize_steps(plan_json.get("steps", []))
    return PlanPreview(
        plan_id=plan.id,
        session_id=plan.session_id,
        goal=plan.goal,
        steps=[PlanStepSchema(**step) for step in steps],
        status=plan.status,
    )


class PlanExecutor:

    @staticmethod
    async def get_plan_or_404(db: AsyncSession, session_id: str, plan_id: str) -> ExecutionPlan:
        result = await db.execute(
            select(ExecutionPlan).where(
                ExecutionPlan.id == plan_id,
                ExecutionPlan.session_id == session_id,
            )
        )
        plan = result.scalar_one_or_none()
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        return plan

    @staticmethod
    async def generate_plan(
        db: AsyncSession,
        session: Session,
        agent_snapshot: dict,
        goal: str,
        user_id: str,
    ) -> PlanPreview:
        model_cfg = agent_snapshot.get("model_config_") or agent_snapshot.get("model_config") or {}
        llm = await PlanExecutor._get_llm(db, model_cfg)

        runner = Runner(
            id=str(uuid.uuid4()),
            session_id=session.id,
            agent_id=session.agent_id,
            agent_version=agent_snapshot.get("version", 1),
            user_id=user_id,
            input_data={"goal": goal, "mode": "plan_generate"},
        )
        db.add(runner)
        await db.flush()

        tracer = Tracer(db=db, runner=runner)
        span = await tracer.start_span(
            span_type="llm_call",
            name="Plan Generation",
            snapshot={"goal": goal, "mode": "plan_generate"},
        )

        messages = [{"role": "user", "content": f"Goal: {goal}\n\nGenerate a structured execution plan."}]
        params = {k: v for k, v in model_cfg.items() if k not in ("provider_id", "provider")}
        params["system"] = PLAN_SYSTEM_PROMPT

        response = await llm.chat(messages, None, params)
        try:
            parsed = parse_llm_json(response.content)
            steps = normalize_steps(parsed.get("steps", []))
            plan_goal = parsed.get("goal") or goal
        except Exception:
            plan_goal = goal
            steps = normalize_steps([{"id": "s1", "description": goal, "depends_on": []}])

        await tracer.end_span(
            span,
            request={"goal": goal},
            response={"goal": plan_goal, "steps": steps},
            output_tokens=max(len(response.content) // 4, 1),
        )
        await tracer.end_runner(output={"goal": plan_goal, "steps": steps})

        plan_record = ExecutionPlan(
            id=str(uuid.uuid4()),
            session_id=session.id,
            goal=plan_goal,
            plan_json={"goal": plan_goal, "steps": steps},
            status="draft",
        )
        db.add(plan_record)

        if not session.title:
            session.title = plan_goal[:120]
            session.mode = "plan"
        await db.commit()
        await db.refresh(plan_record)

        return plan_to_preview(plan_record)

    @staticmethod
    async def update_plan(
        db: AsyncSession,
        session_id: str,
        plan_id: str,
        steps: list[dict],
    ) -> PlanPreview:
        plan = await PlanExecutor.get_plan_or_404(db, session_id, plan_id)
        if plan.status not in ("draft", "confirmed"):
            raise HTTPException(status_code=400, detail="Plan cannot be edited in current status")

        normalized = normalize_steps([step if isinstance(step, dict) else step.model_dump() for step in steps])
        plan.plan_json = {**plan.plan_json, "steps": normalized}
        plan.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(plan)
        return plan_to_preview(plan)

    @staticmethod
    async def revise_plan(
        db: AsyncSession,
        session: Session,
        agent_snapshot: dict,
        plan_id: str,
        instruction: str,
        user_id: str,
    ) -> PlanPreview:
        plan = await PlanExecutor.get_plan_or_404(db, session.id, plan_id)
        if plan.status not in ("draft", "confirmed"):
            raise HTTPException(status_code=400, detail="Plan cannot be revised in current status")

        model_cfg = agent_snapshot.get("model_config_") or agent_snapshot.get("model_config") or {}
        llm = await PlanExecutor._get_llm(db, model_cfg)

        current_plan = json.dumps(plan.plan_json, ensure_ascii=False, indent=2)
        messages = [{
            "role": "user",
            "content": (
                f"Current plan:\n{current_plan}\n\n"
                f"User instruction: {instruction}\n\n"
                "Return the revised plan JSON."
            ),
        }]
        params = {k: v for k, v in model_cfg.items() if k not in ("provider_id", "provider")}
        params["system"] = PLAN_REVISE_PROMPT

        response = await llm.chat(messages, None, params)
        try:
            parsed = parse_llm_json(response.content)
            steps = normalize_steps(parsed.get("steps", plan.plan_json.get("steps", [])))
            plan_goal = parsed.get("goal") or plan.goal
        except Exception:
            raise HTTPException(status_code=422, detail="Failed to parse revised plan from LLM")

        plan.goal = plan_goal
        plan.plan_json = {"goal": plan_goal, "steps": steps}
        plan.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(plan)
        return plan_to_preview(plan)

    @staticmethod
    async def execute_plan(
        db: AsyncSession,
        session: Session,
        agent_snapshot: dict,
        plan_id: str,
    ) -> str:
        from app.models.task import Task
        from app.models.agent import Agent

        plan = await PlanExecutor.get_plan_or_404(db, session.id, plan_id)
        if plan.status not in ("draft", "confirmed"):
            raise HTTPException(status_code=400, detail="Plan has already been executed")

        result = await db.execute(select(Agent).where(Agent.id == session.agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        plan_payload = {
            "plan_id": plan.id,
            "goal": plan.goal,
            "steps": normalize_steps(plan.plan_json.get("steps", [])),
        }

        task = Task(
            id=str(uuid.uuid4()),
            agent_id=session.agent_id,
            agent_version=agent.version,
            user_id=session.user_id,
            session_id=session.id,
            title=plan.goal,
            trigger_type="immediate",
            input_data={"type": "plan", **plan_payload},
            output_channel="platform_ui",
        )
        db.add(task)
        await db.flush()

        plan.status = "confirmed"
        plan.task_id = task.id
        await db.commit()

        from app.workers.task_worker import execute_plan_task
        celery_res = execute_plan_task.delay(task.id, plan_payload)

        # Only update task — avoid flushing a stale in-memory plan (eager mode
        # may have already moved plan to completed in the worker).
        from sqlalchemy import update
        from app.models.task import Task as TaskModel
        await db.execute(
            update(TaskModel).where(TaskModel.id == task.id).values(celery_task_id=celery_res.id)
        )
        await db.commit()

        await broadcast_session_event(session.id, "plan_started", {
            "plan_id": plan.id,
            "task_id": task.id,
            "goal": plan.goal,
        })
        return task.id

    @staticmethod
    async def _get_llm(db: AsyncSession, model_cfg: dict):
        provider_id = model_cfg.get("provider_id")
        if not provider_id:
            raise ValueError("No provider_id in model_config")
        result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
        provider = result.scalar_one_or_none()
        if not provider:
            raise ValueError(f"Model provider {provider_id} not found")
        return ModelHub.get(
            model_cfg,
            provider_type=provider.provider,
            api_key=provider.api_key_encrypted or "",
            base_url=provider.base_url,
        )
