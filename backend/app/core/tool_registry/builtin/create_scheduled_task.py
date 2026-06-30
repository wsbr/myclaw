from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext


class CreateScheduledTaskTool(BaseTool):
    name = "create_scheduled_task"
    description = "创建一个定时执行的任务（cron 表达式），比如每天早上8点发送报告"
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "任务标题"},
            "description": {"type": "string", "description": "任务描述"},
            "cron_expr": {"type": "string", "description": "Cron 表达式，如 '0 8 * * *' 表示每天8点"},
            "output_channel": {"type": "string", "description": "结果输出渠道"},
        },
        "required": ["title", "description", "cron_expr"],
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        from app.models.task import Task
        from app.models.agent import Agent
        from sqlalchemy import select
        import uuid
        from datetime import datetime
        from croniter import croniter

        try:
            cron = croniter(args["cron_expr"])
            next_run = cron.get_next(datetime)
        except Exception as e:
            return ToolResult(success=False, error=f"Invalid cron expression: {e}")

        result = await db.execute(select(Agent).where(Agent.id == context.agent_id))
        agent = result.scalar_one_or_none()

        task = Task(
            id=str(uuid.uuid4()),
            agent_id=context.agent_id,
            agent_version=agent.version,
            user_id=context.user_id,
            session_id=context.session_id,
            title=args["title"],
            trigger_type="scheduled",
            cron_expr=args["cron_expr"],
            scheduled_at=next_run,
            input_data={"description": args["description"]},
            output_channel=args.get("output_channel", context.output_channel),
        )
        db.add(task)
        await db.commit()

        return ToolResult(success=True, data={
            "task_id": task.id,
            "title": task.title,
            "next_run": next_run.isoformat(),
            "cron_expr": args["cron_expr"],
        })
