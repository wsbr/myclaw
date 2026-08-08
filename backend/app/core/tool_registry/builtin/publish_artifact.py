from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.services.artifact_service import artifact_view, publish_artifact


class PublishArtifactTool(BaseTool):
    name = "publish_artifact"
    description = (
        "WHEN: publish a stable Workspace file as an immutable, downloadable Artifact. "
        "WHEN NOT: do not publish a missing or still-changing file and do not use this to edit content. "
        "RETURNS: Artifact ID, name, checksum, size, provenance, and optional parent version. "
        "RECOVERY: verify the Workspace file/checksum, then publish a new immutable version."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
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
        artifact = await publish_artifact(db, context=context, args=args)
        data = {"artifact": artifact_view(artifact)}
        return ToolResult(
            status="succeeded",
            data=data,
            summary=f"Artifact published: {artifact.name} ({artifact.id})",
            content_blocks=[{"type": "artifact_reference", "artifact_id": artifact.id}],
            artifact_ids=[artifact.id],
            evidence=[{
                "kind": "artifact", "artifact_id": artifact.id,
                "checksum": artifact.checksum, "source_path": args["path"],
            }],
        )
