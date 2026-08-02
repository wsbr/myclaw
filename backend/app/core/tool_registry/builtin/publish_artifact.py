from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.services.artifact_service import artifact_view, publish_artifact


class PublishArtifactTool(BaseTool):
    name = "publish_artifact"
    description = "将当前 Workspace 中的稳定文件显式登记为不可覆盖的 Artifact；可发布到当前 Group"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "name": {"type": "string", "maxLength": 500},
            "mime_type": {"type": "string", "maxLength": 255},
            "parent_artifact_id": {"type": "string"},
            "publish_to_group": {"type": "boolean", "default": False},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        if db is None:
            return ToolResult(success=False, error="database connection is required")
        async with db.begin():
            artifact = await publish_artifact(db, context=context, args=args)
        return ToolResult(success=True, data={"artifact": artifact_view(artifact)})
