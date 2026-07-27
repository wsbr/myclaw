from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext


class CreateTaskTool(BaseTool):
    name = "create_task"
    description = "在后台异步创建并执行一个任务，不阻塞当前对话"
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "任务标题"},
            "description": {"type": "string", "description": "任务详细描述和输入参数"},
            "output_channel": {"type": "string", "description": "结果输出渠道", "default": "platform_ui"},
        },
        "required": ["title", "description"],
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        from app.models.task import Task
        from app.models.agent import Agent
        from sqlalchemy import select
        import uuid
        from datetime import datetime

        result = await db.execute(select(Agent).where(Agent.id == context.agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            return ToolResult(success=False, error="Agent not found")

        task = Task(
            id=str(uuid.uuid4()),
            agent_id=context.agent_id,
            agent_version=agent.version,
            user_id=context.user_id,
            session_id=context.session_id,
            parent_task_id=context.task_id,
            title=args["title"],
            trigger_type="immediate",
            input_data={"description": args["description"]},
            output_channel=args.get("output_channel", context.output_channel),
        )
        db.add(task)
        await db.flush()

        from app.workers.task_worker import execute_task
        celery_result = execute_task.delay(task.id)
        task.celery_task_id = celery_result.id
        await db.commit()

        return ToolResult(success=True, data={"task_id": task.id, "title": task.title, "status": "running"})
