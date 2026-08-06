from __future__ import annotations

from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.services.workspace_file_service import (
    atomic_write, resolve_artifact_storage_path, resolve_workspace_path, sha256_file,
)


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "以原子写入方式在当前 Workspace 创建或替换文件；默认不发布 Artifact"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "source_artifact_id": {"type": "string"},
            "mode": {"type": "string", "enum": ["create", "replace"]},
            "expected_checksum": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "required": ["path", "mode"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        if db is None:
            return ToolResult(success=False, error="database connection is required")
        has_content = isinstance(args.get("content"), str)
        has_artifact = isinstance(args.get("source_artifact_id"), str) and bool(args.get("source_artifact_id"))
        if has_content == has_artifact:
            return ToolResult(success=False, error="exactly one of content or source_artifact_id is required")
        mode = args.get("mode")
        if mode not in {"create", "replace"}:
            return ToolResult(success=False, error="mode must be create or replace")
        _, path, relative = await resolve_workspace_path(
            db, workspace_id=context.workspace_id, session_id=context.session_id,
            relative_path=args["path"], agent_id=context.agent_id,
            group_session_id=context.group_session_id,
        )
        exists = path.exists()
        if exists and not path.is_file():
            return ToolResult(success=False, error="target exists and is not a regular file")
        if mode == "create" and exists:
            return ToolResult(success=False, error="version conflict: target already exists")
        if mode == "replace" and not exists:
            return ToolResult(success=False, error="version conflict: target does not exist")
        expected = args.get("expected_checksum")
        if expected is not None:
            if not exists or sha256_file(path) != expected:
                return ToolResult(success=False, error="version conflict: checksum does not match")
        if has_artifact:
            source, artifact = await resolve_artifact_storage_path(
                db, context=context, artifact_id=args["source_artifact_id"],
            )
            if artifact.size_bytes > 50 * 1024 * 1024:
                return ToolResult(success=False, error="source Artifact exceeds the 50 MiB copy limit")
            content = source.read_bytes()
        else:
            content = args["content"].encode("utf-8")
            if len(content) > 10 * 1024 * 1024:
                return ToolResult(success=False, error="content exceeds the 10 MiB inline write limit")
        atomic_write(path, content)
        result = {
            "path": relative, "size": len(content), "checksum": sha256_file(path),
        }
        if has_artifact:
            result["source_artifact_id"] = artifact.id
        return ToolResult(success=True, data=result)
