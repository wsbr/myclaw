import asyncio
from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext


class InvokeAgentTool(BaseTool):
    name = "invoke_agent"
    description = "向工作网中的另一个 Agent 发起协作请求，等待其完成并返回结果"
    input_schema = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "目标 Agent 的 ID"},
            "task": {"type": "string", "description": "要委托给该 Agent 的任务描述"},
            "input": {"type": "object", "description": "任务输入参数（可选）"},
        },
        "required": ["agent_id", "task"],
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        target_id = args["agent_id"]

        # 防循环检测
        if target_id in context.call_chain:
            return ToolResult(success=False, error=f"Circular agent call detected: {target_id}")
        if len(context.call_chain) >= context.max_depth:
            return ToolResult(success=False, error="Maximum call depth exceeded")

        from app.models.task import Task
        from app.models.agent import Agent
        from sqlalchemy import select
        import uuid
        from datetime import datetime

        result = await db.execute(select(Agent).where(Agent.id == target_id))
        agent = result.scalar_one_or_none()
        if not agent:
            return ToolResult(success=False, error=f"Agent {target_id} not found")

        # Mark current task as waiting
        if context.task_id:
            from app.services.task_service import TaskService
            await TaskService.set_waiting(db, context.task_id, f"等待 {agent.name} 返回结果")

        # Create sub-task (shares session_id)
        sub_task = Task(
            id=str(uuid.uuid4()),
            agent_id=target_id,
            agent_version=agent.version,
            user_id=context.user_id,
            session_id=context.session_id,
            parent_task_id=context.task_id,
            title=args["task"][:200],
            trigger_type="immediate",
            input_data={"description": args["task"], **(args.get("input") or {})},
            output_channel=context.output_channel,
        )
        db.add(sub_task)
        await db.commit()

        # Dispatch sub-task and wait
        from app.workers.task_worker import execute_task
        celery_res = execute_task.delay(sub_task.id)
        sub_task.celery_task_id = celery_res.id
        await db.commit()

        # Poll for completion (max timeout seconds)
        timeout = context.timeout
        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(2)
            elapsed += 2
            await db.refresh(sub_task)
            if sub_task.status in ("completed", "failed", "cancelled"):
                break

        # Resume current task
        if context.task_id:
            from app.services.task_service import TaskService
            await TaskService.set_running(db, context.task_id)

        if sub_task.status != "completed":
            return ToolResult(success=False, error=f"Sub-task ended with status: {sub_task.status}")

        return ToolResult(success=True, data=sub_task.output_data)
