from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext
from app.core.schedule.parser import resolve_scheduled_at


class CreateScheduledTaskTool(BaseTool):
    name = "create_scheduled_task"
    description = (
        "创建一个延时或定时执行的后台任务。用户说「N分钟后提醒/通知」时用 delay；"
        "用户说「每天8点」等周期性需求时用 cron_expr。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "任务标题"},
            "description": {
                "type": "string",
                "description": (
                    "到点时 Agent 要执行的任务内容本身，只写「做什么」（如：提醒用户复习XX、生成并发送某段代码）。"
                    "禁止在此字段中包含任何时间/延时信息（如「5分钟后」「明天」「每天8点」等）——"
                    "所有时间信息已经由 delay/cron_expr/at 参数完全表达，本字段内容会在到点时原样交给"
                    "Agent 执行，不会再被重新解析出时间含义。"
                ),
            },
            "delay": {
                "type": "string",
                "description": "相对延时，一次性执行。如 '5m'（5分钟）、'1h'（1小时）、'30s'（30秒）",
            },
            "cron_expr": {
                "type": "string",
                "description": "Cron 表达式，周期性执行。如 '0 8 * * *' 表示每天8点",
            },
            "at": {
                "type": "string",
                "description": "绝对执行时间 ISO8601，如 '2026-07-10T20:00:00'",
            },
            "output_channel": {"type": "string", "description": "结果输出渠道，默认 platform_ui"},
        },
        "required": ["title", "description"],
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        from app.models.task import Task
        from app.models.agent import Agent
        from sqlalchemy import select
        import uuid

        delay = args.get("delay")
        cron_expr = args.get("cron_expr")
        at = args.get("at")

        if not any([delay, cron_expr, at]):
            return ToolResult(
                success=False,
                error="Must provide one of: delay (e.g. '5m'), cron_expr, or at (ISO8601)",
            )

        try:
            scheduled_at, resolved_cron = resolve_scheduled_at(
                delay=delay,
                cron_expr=cron_expr,
                at=at,
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        result = await db.execute(select(Agent).where(Agent.id == context.agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            return ToolResult(success=False, error="Agent not found")

        if context.task_id:
            from app.services.task_service import TaskService
            depth = await TaskService.count_ancestor_chain_depth(db, context.task_id)
            if depth >= TaskService.MAX_SCHEDULED_CHAIN_DEPTH:
                return ToolResult(
                    success=False,
                    error=(
                        f"Refusing to create another scheduled task: this task chain is already "
                        f"{depth} levels deep (max {TaskService.MAX_SCHEDULED_CHAIN_DEPTH}). This usually "
                        f"means a scheduled task is re-scheduling itself instead of executing its content. "
                        f"Execute the task content directly instead of calling create_scheduled_task again."
                    ),
                )

        task = Task(
            id=str(uuid.uuid4()),
            agent_id=context.agent_id,
            agent_version=agent.version,
            user_id=context.user_id,
            session_id=context.session_id,
            parent_task_id=context.task_id,
            title=args["title"],
            trigger_type="scheduled",
            cron_expr=resolved_cron,
            scheduled_at=scheduled_at,
            input_data={"description": args["description"]},
            output_channel=args.get("output_channel", context.output_channel),
        )
        db.add(task)
        await db.commit()

        data = {
            "task_id": task.id,
            "title": task.title,
            "scheduled_at": scheduled_at.isoformat(),
        }
        if resolved_cron:
            data["cron_expr"] = resolved_cron
        if delay:
            data["delay"] = delay

        return ToolResult(success=True, data=data)
