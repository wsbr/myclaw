from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext


class SearchTasksTool(BaseTool):
    name = "search_tasks"
    description = "查询当前用户的任务列表，可按状态筛选"
    input_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pending", "running", "waiting", "completed", "failed", "cancelled"],
                "description": "任务状态筛选（可选）",
            },
            "limit": {"type": "integer", "description": "返回数量限制", "default": 10},
        },
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        from app.models.task import Task
        from sqlalchemy import select

        q = select(Task).where(Task.user_id == context.user_id)
        if status := args.get("status"):
            q = q.where(Task.status == status)
        q = q.order_by(Task.created_at.desc()).limit(args.get("limit", 10))

        result = await db.execute(q)
        tasks = result.scalars().all()

        return ToolResult(success=True, data=[{
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "waiting_reason": t.waiting_reason,
            "output_channel": t.output_channel,
            "created_at": t.created_at.isoformat(),
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        } for t in tasks])
