from __future__ import annotations

from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.services.workspace_file_service import resolve_read_path, sha256_file


class ReadFileTool(BaseTool):
    name = "read_file"
    description = (
        "WHEN: read a bounded page from an authorized Workspace, frozen Skill, or Artifact file. "
        "WHEN NOT: use list_files/search_files to discover paths first; never infer missing content. "
        "RETURNS: decoded content, checksum, byte offsets, size, and truncation state. "
        "RECOVERY: continue at next_offset or choose the correct encoding."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 65536, "default": 65536},
            "encoding": {"type": "string", "enum": ["utf-8", "utf-16", "latin-1"], "default": "utf-8"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        if db is None:
            return ToolResult(success=False, error="database connection is required")
        offset = args.get("offset", 0)
        limit = args.get("limit", 65536)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return ToolResult(success=False, error="offset must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 65536:
            return ToolResult(success=False, error="limit must be between 1 and 65536")
        encoding = args.get("encoding", "utf-8")
        if encoding not in {"utf-8", "utf-16", "latin-1"}:
            return ToolResult(success=False, error="unsupported encoding")
        path, display_path = await resolve_read_path(db, context=context, value=args["path"])
        if not path.is_file():
            return ToolResult(success=False, error="file does not exist")
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(limit)
        try:
            content = chunk.decode(encoding)
        except UnicodeDecodeError:
            return ToolResult(success=False, error=f"file content is not valid {encoding}")
        next_offset = offset + len(chunk)
        data = {
            "path": display_path,
            "content": content,
            "truncated": next_offset < size,
            "next_offset": next_offset if next_offset < size else None,
            "size": size,
            "checksum": sha256_file(path),
        }
        return ToolResult(
            status="succeeded", summary=f"Read {len(chunk)} bytes from {display_path}",
            data=data, content_blocks=[{"type": "text", "text": content}],
            evidence=[{"kind": "file", "path": display_path, "checksum": data["checksum"], "byte_offset": offset, "byte_length": len(chunk)}],
        )
