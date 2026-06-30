from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext


class GetTaskStatusTool(BaseTool):
    name = "get_task_status"
    description = "查询某个 Task 的当前执行状态和输出结果"
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "要查询的 Task ID"},
        },
        "required": ["task_id"],
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        task_id = args.get("task_id")
        if not task_id:
            return ToolResult(success=False, data=None, error="task_id is required")

        if db is None:
            return ToolResult(success=False, data=None, error="No database connection")

        from app.models.task import Task
        from sqlalchemy import select

        result = await db.execute(
            select(Task).where(Task.id == task_id, Task.user_id == context.user_id)
        )
        task = result.scalar_one_or_none()
        if not task:
            return ToolResult(success=False, data=None, error=f"Task {task_id} not found")

        return ToolResult(success=True, data={
            "task_id": task.id,
            "status": task.status,
            "waiting_reason": task.waiting_reason,
            "output": task.output_data,
            "created_at": str(task.created_at),
            "started_at": str(task.started_at) if task.started_at else None,
            "completed_at": str(task.completed_at) if task.completed_at else None,
        })
