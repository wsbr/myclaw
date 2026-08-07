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
        return ToolResult(
            success=False,
            error="invoke_agent must be dispatched by the durable runtime AgentInvocation gateway",
        )
