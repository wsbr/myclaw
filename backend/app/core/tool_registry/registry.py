from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext
from app.core.tool_registry.builtin.invoke_agent import InvokeAgentTool
from app.core.tool_registry.builtin.request_clarification import RequestClarificationTool
from app.core.tool_registry.builtin.read_file import ReadFileTool
from app.core.tool_registry.builtin.write_file import WriteFileTool
from app.core.tool_registry.builtin.execute import ExecuteTool
from app.core.tool_registry.builtin.search_group_history import SearchGroupHistoryTool
from app.core.tool_registry.builtin.publish_artifact import PublishArtifactTool

BUILTIN_TOOLS: dict[str, BaseTool] = {
    t.name: t()
    for t in [
        InvokeAgentTool, RequestClarificationTool,
        ReadFileTool, WriteFileTool, ExecuteTool, SearchGroupHistoryTool,
        PublishArtifactTool,
    ]
}


def _missing_required_fields(tool: BaseTool, args: dict) -> list[str]:
    required = tool.input_schema.get("required") or []
    return [f for f in required if f not in args or args.get(f) is None]


class ToolRegistry:

    @staticmethod
    async def execute(tool_name: str, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        tool = BUILTIN_TOOLS.get(tool_name)
        if not tool:
            return ToolResult(success=False, error=f"Tool '{tool_name}' not found")

        missing = _missing_required_fields(tool, args)
        if missing:
            required = tool.input_schema.get("required") or []
            return ToolResult(
                success=False,
                error=(
                    f"Missing required field(s): {', '.join(missing)}. "
                    f"You provided: {list(args.keys())}. "
                    f"Expected fields: {', '.join(required)}"
                ),
            )

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
