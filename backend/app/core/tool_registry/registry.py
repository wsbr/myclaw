from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext
from app.core.tool_registry.builtin.create_task import CreateTaskTool
from app.core.tool_registry.builtin.create_scheduled_task import CreateScheduledTaskTool
from app.core.tool_registry.builtin.search_tasks import SearchTasksTool
from app.core.tool_registry.builtin.send_to_channel import SendToChannelTool
from app.core.tool_registry.builtin.emit_event import EmitEventTool
from app.core.tool_registry.builtin.find_agents import FindAgentsTool
from app.core.tool_registry.builtin.get_agent_schema import GetAgentSchemaTool
from app.core.tool_registry.builtin.invoke_agent import InvokeAgentTool
from app.core.tool_registry.builtin.get_task_status import GetTaskStatusTool

BUILTIN_TOOLS: dict[str, BaseTool] = {
    t.name: t()
    for t in [
        CreateTaskTool, CreateScheduledTaskTool, SearchTasksTool,
        SendToChannelTool, EmitEventTool, FindAgentsTool,
        GetAgentSchemaTool, InvokeAgentTool, GetTaskStatusTool,
    ]
}


class ToolRegistry:

    @staticmethod
    async def execute(tool_name: str, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        tool = BUILTIN_TOOLS.get(tool_name)
        if not tool:
            return ToolResult(success=False, error=f"Tool '{tool_name}' not found")
        try:
            return await tool.execute(args, context, db=db)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    @staticmethod
    def get_definitions(tool_keys: list[str]) -> list[dict]:
        return [
            BUILTIN_TOOLS[k].to_llm_definition()
            for k in tool_keys
            if k in BUILTIN_TOOLS
        ]

    @staticmethod
    def all_builtin_keys() -> list[str]:
        return list(BUILTIN_TOOLS.keys())
