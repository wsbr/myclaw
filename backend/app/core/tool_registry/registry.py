from app.core.tool_registry.base import BaseTool, ToolResult, RuntimeContext
from app.core.tool_registry.schema import (
    builtin_result_schema, validation_error_refs, validation_errors,
)
from app.core.tool_registry.builtin.read_file import ReadFileTool
from app.core.tool_registry.builtin.write_file import WriteFileTool
from app.core.tool_registry.builtin.execute import ExecuteTool
from app.core.tool_registry.builtin.search_group_history import SearchGroupHistoryTool
from app.core.tool_registry.builtin.publish_artifact import PublishArtifactTool
from app.core.tool_registry.builtin.workspace_catalog import (
    ApplyPatchTool, DeletePathTool, ListFilesTool, SearchFilesTool,
)
from app.core.tool_registry.builtin.memory_tools import MemoryProposeTool, MemorySearchTool
from app.core.tool_registry.builtin.knowledge_search import KnowledgeSearchTool
from app.core.tool_registry.builtin.schedule_task import ScheduleTaskTool
from app.core.tool_registry.builtin.web_tools import WebFetchTool, WebSearchTool
from app.core.tool_registry.builtin.browser_tools import (
    BrowserInteractTool, BrowserNavigateTool, BrowserObserveTool, BrowserScreenshotTool,
)

BUILTIN_TOOLS: dict[str, BaseTool] = {
    t.name: t()
    for t in [
        ReadFileTool, WriteFileTool, ExecuteTool, SearchGroupHistoryTool,
        PublishArtifactTool, ListFilesTool, SearchFilesTool, ApplyPatchTool,
        DeletePathTool, MemorySearchTool, MemoryProposeTool,
        KnowledgeSearchTool,
        ScheduleTaskTool,
        WebSearchTool, WebFetchTool,
        BrowserNavigateTool, BrowserObserveTool, BrowserInteractTool, BrowserScreenshotTool,
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
            if db is not None and context.tool_version_id and context.tool_connection_revision_id:
                from app.services.mcp_tool_service import call_mcp_tool
                from app.models.registry import ToolVersion
                try:
                    result = await call_mcp_tool(
                        db, tool_version_id=context.tool_version_id,
                        connection_revision_id=context.tool_connection_revision_id,
                        arguments=args,
                    )
                    version = await db.get(ToolVersion, context.tool_version_id)
                    issues = validation_error_refs(
                        result.model_dump(mode="json"), version.output_schema,
                    ) if version is not None else [{"message": "ToolVersion unavailable"}]
                    if issues:
                        return ToolResult(
                            status="failed", summary="MCP Tool returned an invalid result",
                            error_code="tool_output_schema_error",
                            error="MCP Tool output failed its frozen ToolVersion schema",
                            error_category="provider", error_details={"errors": issues},
                        )
                    return result
                except Exception as error:
                    return ToolResult(
                        status="failed", summary=f"MCP execution failed: {type(error).__name__}",
                        error=f"MCP execution failed: {type(error).__name__}",
                        error_code="mcp_execution_unhandled", error_category="provider",
                    )
            return ToolResult(status="failed", error=f"Tool '{tool_name}' not found", error_code="tool_not_found", error_category="validation")

        issues = validation_errors(args, tool.input_schema)
        if issues:
            return ToolResult(
                status="failed", summary="Tool arguments failed schema validation",
                error_code="tool_arguments_schema_error",
                error="; ".join(f"{item['path']}: {item['message']}" for item in issues),
                error_category="validation", error_details={"errors": issues},
            )

        try:
            result = await tool.execute(args, context, db=db)
            output_issues = validation_error_refs(
                result.model_dump(mode="json"), builtin_result_schema(tool.name),
            )
            if output_issues:
                return ToolResult(
                    status="failed", summary="Tool returned an invalid result",
                    error_code="tool_output_schema_error",
                    error="Tool output failed its immutable ToolVersion schema",
                    error_category="provider", error_details={"errors": output_issues},
                )
            return result
        except Exception as error:
            from app.services.workspace_file_service import WorkspaceFileError
            from app.services.memory_service import MemoryConflict
            from app.services.artifact_service import ArtifactPublishError
            from app.services.knowledge_service import KnowledgeError
            if isinstance(error, WorkspaceFileError):
                return ToolResult(
                    status="failed", summary=str(error), error=str(error),
                    error_code=error.code, error_category=error.category,
                    retryable=error.category in {"conflict", "transient"},
                )
            if isinstance(error, MemoryConflict):
                return ToolResult(
                    status="failed", summary=str(error), error=str(error),
                    error_code="memory_proposal_conflict", error_category="conflict",
                    retryable=False,
                )
            if isinstance(error, ArtifactPublishError):
                return ToolResult(
                    status="failed", summary=str(error), error=str(error),
                    error_code="artifact_publish_conflict", error_category="conflict",
                    retryable=False,
                )
            if isinstance(error, KnowledgeError):
                return ToolResult(
                    status="failed", summary=str(error), error=str(error),
                    error_code=error.code,
                    error_category=(
                        "authorization" if error.status_code in {401, 403, 404}
                        else "validation"
                    ),
                    retryable=False,
                )
            return ToolResult(
                status="failed",
                summary=f"Tool execution failed: {type(error).__name__}",
                error=f"Tool execution failed: {type(error).__name__}",
                error_code="tool_execution_unhandled",
                error_category="provider",
            )

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
