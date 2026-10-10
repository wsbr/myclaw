from __future__ import annotations

from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.services.workspace_file_service import (
    atomic_write, resolve_artifact_storage_path, resolve_workspace_path, sha256_file,
)


class WriteFileTool(BaseTool):
    name = "write_file"
    description = (
        "WHEN: atomically create or replace one complete Workspace file from inline content or an Artifact. "
        "WHEN NOT: use apply_patch for precise multi-file edits; writing does not publish an Artifact. "
        "RETURNS: final path, size, checksum, and provenance. "
        "RECOVERY: refresh checksum after a conflict and retry with an explicit mode."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "source_artifact_id": {"type": "string"},
            "mode": {"type": "string", "enum": ["create", "replace"]},
            "expected_checksum": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "required": ["path", "mode"],
        "allOf": [
            {
                "if": {"properties": {"mode": {"const": "replace"}}},
                "then": {"required": ["expected_checksum"]},
            }
        ],
        "oneOf": [
            {"required": ["content"], "not": {"required": ["source_artifact_id"]}},
            {"required": ["source_artifact_id"], "not": {"required": ["content"]}},
        ],
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
        # These conflicts require new arguments or a refreshed checksum. A
        # Worker replay of the same immutable invocation cannot resolve them.
        if mode == "create" and exists:
            return ToolResult(
                status="failed", error="version conflict: target already exists",
                error_code="workspace_target_exists", error_category="conflict",
                retryable=False,
            )
        if mode == "replace" and not exists:
            return ToolResult(
                status="failed", error="version conflict: target does not exist",
                error_code="workspace_target_missing", error_category="conflict",
                retryable=False,
            )
        if mode == "replace" and not args.get("expected_checksum"):
            return ToolResult(
                status="failed", error="replace requires expected_checksum CAS",
                error_code="workspace_checksum_required",
                error_category="validation", retryable=False,
            )
        expected = args.get("expected_checksum")
        if expected is not None:
            if not exists or sha256_file(path) != expected:
                return ToolResult(
                    status="failed", error="version conflict: checksum does not match",
                    error_code="workspace_checksum_conflict", error_category="conflict",
                    retryable=False,
                )
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
        return ToolResult(
            status="succeeded", summary=f"Wrote {len(content)} bytes to {relative}",
            data=result, content_blocks=[{"type": "json", "value": result}],
            evidence=[{"kind": "workspace_write", **result}],
        )
