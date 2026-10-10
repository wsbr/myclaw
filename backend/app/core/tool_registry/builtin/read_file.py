from __future__ import annotations

import codecs

from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.services.workspace_file_service import resolve_read_path, sha256_file


class _InvalidOffset(ValueError):
    pass


def _read_text_page(handle, *, offset: int, limit: int, encoding: str) -> tuple[str, int]:
    """Read at an exact byte boundary, extending at most three bytes for a scalar.

    UTF-16 byte order belongs to the file, not each independently decoded page.
    The initial BOM counts as consumed bytes but is not returned as text.
    """
    codec = encoding
    if encoding == "utf-16":
        handle.seek(0)
        bom = handle.read(2)
        if bom not in {codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE}:
            raise UnicodeError("UTF-16 files must begin with a byte-order mark")
        if offset % 2:
            raise _InvalidOffset("offset must be at a UTF-16 character boundary")
        if offset:
            codec = "utf-16-le" if bom == codecs.BOM_UTF16_LE else "utf-16-be"
            handle.seek(offset)
            first_unit = handle.read(2)
            byte_order = "little" if codec == "utf-16-le" else "big"
            if len(first_unit) == 2 and 0xDC00 <= int.from_bytes(first_unit, byte_order) <= 0xDFFF:
                raise _InvalidOffset("offset points inside a UTF-16 surrogate pair")
    elif encoding == "utf-8" and offset:
        handle.seek(offset)
        first_byte = handle.read(1)
        if first_byte and 0x80 <= first_byte[0] <= 0xBF:
            raise _InvalidOffset("offset points inside a UTF-8 character")

    handle.seek(offset)
    chunk = handle.read(limit)
    decoder = codecs.getincrementaldecoder(codec)(errors="strict")
    content = decoder.decode(chunk, final=False)
    consumed = len(chunk)
    # UTF-8 scalars and UTF-16 surrogate pairs are at most four bytes. Do not
    # decode with errors=ignore or round the starting offset: either loses text.
    for _ in range(3):
        if not decoder.getstate()[0]:
            break
        extra = handle.read(1)
        if not extra:
            break
        consumed += len(extra)
        content += decoder.decode(extra, final=False)
    content += decoder.decode(b"", final=True)
    return content, consumed


class ReadFileTool(BaseTool):
    name = "read_file"
    description = (
        "WHEN: read a bounded page from an authorized Workspace, frozen Skill, or Artifact file. "
        "WHEN NOT: do not guess unknown paths; use an available path-discovery capability when needed; never infer missing content. "
        "RETURNS: decoded content, checksum, exact byte offsets, size, and truncation state. "
        "Pages extend at most 3 bytes beyond limit to finish a character; UTF-16 requires a file BOM. "
        "RECOVERY: continue at next_offset; arbitrary offsets must be character boundaries. "
        "Choose the correct encoding after an encoding error."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {
                "type": "integer", "minimum": 0, "default": 0,
                "description": "Exact byte offset at a character boundary; use the previous next_offset.",
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 65536, "default": 65536,
                "description": "Target bytes; at most 3 additional bytes complete the final character.",
            },
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
            return ToolResult(
                status="failed", error="file does not exist",
                error_code="workspace_file_not_found", error_category="validation",
            )
        size = path.stat().st_size
        try:
            with path.open("rb") as handle:
                content, consumed = (
                    _read_text_page(handle, offset=offset, limit=limit, encoding=encoding)
                    if offset < size else ("", 0)
                )
        except _InvalidOffset as error:
            return ToolResult(
                status="failed", error=str(error),
                error_code="workspace_offset_invalid", error_category="validation",
                error_details={"offset": offset, "encoding": encoding},
            )
        except UnicodeError:
            return ToolResult(
                status="failed", error=(
                    f"file content is not valid {encoding}"
                    + ("; UTF-16 requires a file byte-order mark" if encoding == "utf-16" else "")
                ),
                error_code="workspace_encoding_error", error_category="validation",
                error_details={"offset": offset, "encoding": encoding},
            )
        next_offset = offset + consumed
        data = {
            "path": display_path,
            "content": content,
            "truncated": next_offset < size,
            "next_offset": next_offset if next_offset < size else None,
            "size": size,
            "checksum": sha256_file(path),
        }
        return ToolResult(
            status="succeeded", summary=f"Read {consumed} bytes from {display_path}",
            data=data, content_blocks=[{"type": "text", "text": content}],
            evidence=[{"kind": "file", "path": display_path, "checksum": data["checksum"], "byte_offset": offset, "byte_length": consumed}],
        )
