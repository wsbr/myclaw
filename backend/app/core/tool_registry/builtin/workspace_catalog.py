"""Provider-neutral Workspace catalog, patch, and recoverable deletion Tools."""

from __future__ import annotations

import fnmatch
import hashlib
import re
import shutil
from pathlib import Path

from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.models.common import uuid7
from app.services.workspace_file_service import (
    WorkspaceFileError, atomic_write, execution_workspace_root,
    resolve_workspace_path, sha256_file,
)


def _visible_files(root: Path):
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not relative.parts or relative.parts[0] == ".myclaw" or path.is_symlink():
            continue
        yield path, relative.as_posix()


def _compile_search_pattern(query: str, *, regex: bool, case_sensitive: bool):
    flags = 0 if case_sensitive else re.IGNORECASE
    if not regex:
        return re.compile(re.escape(query), flags)
    # Python's backtracking engine has no match timeout. Keep the public regex
    # surface to a safe, useful subset and reject constructs commonly used for
    # catastrophic backtracking or context-dependent matching.
    if len(query) > 256:
        raise WorkspaceFileError("regular expression exceeds the 256 character safety limit")
    high_cost_groups = (
        r"\([^)]*[|][^)]*\)(?:[*+]|\{)",
        r"\([^)]*(?:[*+]\??|\{\d+(?:,\d*)?\})[^)]*\)(?:[*+]|\{)",
    )
    if (
        re.search(r"\\[1-9]", query)
        or any(token in query for token in ("(?=", "(?!", "(?<=", "(?<!", "(?P=", "(?("))
        or any(re.search(marker, query) for marker in high_cost_groups)
    ):
        raise WorkspaceFileError("regular expression uses a prohibited high-cost construct")
    try:
        return re.compile(query, flags)
    except re.error as error:
        raise WorkspaceFileError(f"invalid regular expression: {error}") from error


class ListFilesTool(BaseTool):
    name = "list_files"
    description = (
        "WHEN: inspect a Workspace directory before reading or editing. "
        "WHEN NOT: do not use for file contents; reading or searching contents requires its own available capability. "
        "RETURNS: a stable page of file/directory metadata plus next_cursor. "
        "RECOVERY: continue with next_cursor when truncated."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
            "recursive": {"type": "boolean", "default": False},
            "cursor": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        _, directory, relative = await resolve_workspace_path(
            db, workspace_id=context.workspace_id, session_id=context.session_id,
            relative_path=args.get("path", "."), agent_id=context.agent_id,
            group_session_id=context.group_session_id, allow_dot=True,
        )
        if not directory.is_dir() or directory.is_symlink():
            raise WorkspaceFileError("directory does not exist or is not a regular directory")
        recursive = bool(args.get("recursive", False))
        entries = directory.rglob("*") if recursive else directory.iterdir()
        metadata = []
        catalog_truncated = False
        for item in entries:
            rel = item.relative_to(directory)
            if not rel.parts or rel.parts[0] == ".myclaw" or item.is_symlink():
                continue
            if len(metadata) >= 100_000:
                catalog_truncated = True
                break
            stat = item.stat()
            metadata.append({
                "path": rel.as_posix(), "type": "directory" if item.is_dir() else "file",
                "size": stat.st_size if item.is_file() else None,
                "modified_ns": stat.st_mtime_ns,
                "checksum": None,
            })
        metadata.sort(key=lambda item: (item["type"] != "directory", item["path"]))
        cursor = int(args.get("cursor", 0))
        limit = int(args.get("limit", 100))
        page = metadata[cursor:cursor + limit]
        for entry in page:
            if entry["type"] == "file":
                entry["checksum"] = sha256_file(directory / entry["path"])
        next_cursor = cursor + len(page) if cursor + len(page) < len(metadata) else None
        data = {
            "path": relative, "items": page, "next_cursor": next_cursor,
            "total": len(metadata), "catalog_truncated": catalog_truncated,
        }
        return ToolResult(
            status="succeeded", summary=f"Listed {len(page)} Workspace entries",
            data=data, content_blocks=[{"type": "json", "value": data}],
            evidence=[{"kind": "workspace_listing", "path": relative, "count": len(page)}],
        )


class SearchFilesTool(BaseTool):
    name = "search_files"
    description = (
        "WHEN: locate Workspace paths or text with glob or regular expression filters. "
        "WHEN NOT: do not use for complete file reads. "
        "RETURNS: bounded path/line/column/snippet matches and truncation state. "
        "RECOVERY: narrow path/glob/query if truncated."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
            "query": {"type": "string", "minLength": 1, "maxLength": 1000},
            "glob": {"type": "string", "default": "**/*"},
            "regex": {"type": "boolean", "default": False},
            "case_sensitive": {"type": "boolean", "default": False},
            "search_content": {"type": "boolean", "default": True},
            "max_matches": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        _, root, relative = await resolve_workspace_path(
            db, workspace_id=context.workspace_id, session_id=context.session_id,
            relative_path=args.get("path", "."), agent_id=context.agent_id,
            group_session_id=context.group_session_id, allow_dot=True,
        )
        if not root.is_dir() or root.is_symlink():
            raise WorkspaceFileError("search root is not a regular directory")
        query = args["query"]
        pattern = _compile_search_pattern(
            query, regex=bool(args.get("regex")),
            case_sensitive=bool(args.get("case_sensitive")),
        )
        glob = args.get("glob", "**/*")
        limit = int(args.get("max_matches", 100))
        matches: list[dict] = []
        examined = 0
        examined_bytes = 0
        scan_truncated = False
        for path, rel in _visible_files(root):
            # ``fnmatch('root.txt', '**/*')`` is false on Python even though
            # users reasonably expect the recursive default to include files
            # at the search root. Match both the recursive pattern and its
            # zero-directory form.
            glob_matches = (
                glob in {"*", "**/*"}
                or fnmatch.fnmatch(rel, glob)
                or (glob.startswith("**/") and fnmatch.fnmatch(rel, glob[3:]))
            )
            if not path.is_file() or not glob_matches:
                continue
            if examined >= 10_000 or examined_bytes >= 64 * 1024 * 1024:
                scan_truncated = True
                break
            examined += 1
            size = path.stat().st_size
            examined_bytes += min(size, 4 * 1024 * 1024)
            name_match = pattern.search(rel)
            if name_match:
                matches.append({"path": rel, "kind": "path", "line": None, "column": name_match.start() + 1, "snippet": rel})
            if args.get("search_content", True) and len(matches) < limit and size <= 4 * 1024 * 1024:
                try:
                    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                        found = pattern.search(line[:20_000])
                        if found:
                            matches.append({
                                "path": rel, "kind": "content", "line": line_no,
                                "column": found.start() + 1, "snippet": line[:500],
                            })
                            if len(matches) >= limit:
                                break
                except UnicodeDecodeError:
                    pass
            if len(matches) >= limit:
                break
        data = {
            "root": relative, "matches": matches[:limit],
            "truncated": len(matches) >= limit or scan_truncated,
            "examined_files": examined, "examined_bytes": examined_bytes,
        }
        return ToolResult(
            status="succeeded", summary=f"Found {len(data['matches'])} Workspace matches",
            data=data, content_blocks=[{"type": "json", "value": data}],
            evidence=[{"kind": "workspace_search", "query_sha256": hashlib.sha256(query.encode()).hexdigest(), "examined_files": examined}],
        )


def _parse_unified_diff(diff: str) -> list[tuple[str, list[tuple[int, list[str], list[str]]]]]:
    lines = diff.splitlines(keepends=True)
    files: list[tuple[str, list[tuple[int, list[str], list[str]]]]] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise WorkspaceFileError("invalid unified diff file header")
        target = lines[index][4:].strip().split("\t", 1)[0]
        target = target.removeprefix("b/")
        index += 1
        hunks = []
        while index < len(lines) and not lines[index].startswith("--- "):
            if not lines[index].startswith("@@ "):
                index += 1
                continue
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", lines[index])
            if not match:
                raise WorkspaceFileError("invalid unified diff hunk header")
            old_start = int(match.group(1))
            index += 1
            old_lines: list[str] = []
            new_lines: list[str] = []
            last_prefix: str | None = None
            while index < len(lines) and not lines[index].startswith(("@@ ", "--- ")):
                line = lines[index]
                if line.startswith("\\ No newline"):
                    if last_prefix in {" ", "-"} and old_lines and old_lines[-1].endswith(("\n", "\r")):
                        old_lines[-1] = old_lines[-1].rstrip("\r\n")
                    if last_prefix in {" ", "+"} and new_lines and new_lines[-1].endswith(("\n", "\r")):
                        new_lines[-1] = new_lines[-1].rstrip("\r\n")
                    index += 1
                    continue
                if not line or line[0] not in " +-":
                    raise WorkspaceFileError("invalid unified diff hunk body")
                if line[0] in " -":
                    old_lines.append(line[1:])
                if line[0] in " +":
                    new_lines.append(line[1:])
                last_prefix = line[0]
                index += 1
            hunks.append((old_start, old_lines, new_lines))
        files.append((target, hunks))
    if not files:
        raise WorkspaceFileError("diff contains no file patches")
    return files


class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = (
        "WHEN: apply an exact unified diff to one or more existing Workspace files. "
        "WHEN NOT: whole-file creation or replacement requires its own available capability. "
        "RETURNS: checksums before/after for every file. "
        "RECOVERY: refresh files and regenerate the diff after a context or checksum conflict."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "diff": {"type": "string", "minLength": 1, "maxLength": 2097152},
            "expected_checksums": {
                "type": "object",
                "additionalProperties": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        },
        "required": ["diff", "expected_checksums"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        parsed = _parse_unified_diff(args["diff"])
        expected = args.get("expected_checksums") or {}
        prepared: list[tuple[Path, str, bytes, bytes, str]] = []
        seen: set[str] = set()
        for relative, hunks in parsed:
            if relative in seen:
                raise WorkspaceFileError("a diff may patch each file only once")
            seen.add(relative)
            _, path, normalized = await resolve_workspace_path(
                db, workspace_id=context.workspace_id, session_id=context.session_id,
                relative_path=relative, agent_id=context.agent_id,
                group_session_id=context.group_session_id,
            )
            if not path.is_file() or path.is_symlink():
                raise WorkspaceFileError(f"patch target is not a regular file: {normalized}")
            before = path.read_bytes()
            before_hash = hashlib.sha256(before).hexdigest()
            if normalized not in expected:
                raise WorkspaceFileError(
                    f"checksum precondition is required: {normalized}"
                )
            if expected[normalized] != before_hash:
                raise WorkspaceFileError(f"checksum conflict: {normalized}")
            try:
                current = before.decode("utf-8").splitlines(keepends=True)
            except UnicodeDecodeError as error:
                raise WorkspaceFileError(f"patch target is not UTF-8: {normalized}") from error
            offset = 0
            for old_start, old_lines, new_lines in hunks:
                position = old_start - 1 + offset
                if current[position:position + len(old_lines)] != old_lines:
                    raise WorkspaceFileError(f"patch context conflict: {normalized}")
                current[position:position + len(old_lines)] = new_lines
                offset += len(new_lines) - len(old_lines)
            after = "".join(current).encode()
            prepared.append((path, normalized, before, after, before_hash))
        unexpected = sorted(set(expected) - seen)
        if unexpected:
            raise WorkspaceFileError(
                f"checksum preconditions contain unpatched paths: {', '.join(unexpected[:10])}"
            )
        completed: list[tuple[Path, bytes]] = []
        try:
            for path, _, before, after, _ in prepared:
                atomic_write(path, after)
                completed.append((path, before))
        except Exception:
            for path, before in reversed(completed):
                atomic_write(path, before)
            raise
        changes = [
            {"path": relative, "before_checksum": before_hash, "after_checksum": hashlib.sha256(after).hexdigest(), "size": len(after)}
            for _, relative, _, after, before_hash in prepared
        ]
        return ToolResult(
            status="succeeded", summary=f"Patched {len(changes)} Workspace files",
            data={"files": changes}, content_blocks=[{"type": "json", "value": changes}],
            evidence=[{"kind": "workspace_patch", "files": changes}],
        )


class DeletePathTool(BaseTool):
    name = "delete_path"
    description = (
        "WHEN: remove a Workspace file or directory after explicit confirmation. "
        "WHEN NOT: do not use when replacement is sufficient. "
        "RETURNS: a recoverable trash reference and original checksum. "
        "RECOVERY: preserve the trash reference for administrator-assisted restore."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "expected_checksum": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "recursive": {"type": "boolean", "default": False},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        root = await execution_workspace_root(db, context=context)
        _, path, relative = await resolve_workspace_path(
            db, workspace_id=context.workspace_id, session_id=context.session_id,
            relative_path=args["path"], agent_id=context.agent_id,
            group_session_id=context.group_session_id,
        )
        if not path.exists() or path.is_symlink():
            raise WorkspaceFileError("delete target is unavailable")
        if path.is_dir() and not args.get("recursive") and any(path.iterdir()):
            raise WorkspaceFileError("non-empty directory requires recursive=true")
        checksum = sha256_file(path) if path.is_file() else None
        if args.get("expected_checksum") and checksum != args["expected_checksum"]:
            raise WorkspaceFileError("checksum conflict")
        trash_id = uuid7()
        trash_root = root / ".myclaw" / "trash" / trash_id
        trash_root.mkdir(parents=True, exist_ok=False)
        target = trash_root / path.name
        shutil.move(str(path), str(target))
        restore_ref = f"trash://{context.session_id}/{trash_id}/{path.name}"
        data = {"path": relative, "restore_reference": restore_ref, "checksum": checksum}
        return ToolResult(
            status="succeeded", summary=f"Moved {relative} to controlled Trash",
            data=data, content_blocks=[{"type": "json", "value": data}],
            evidence=[{"kind": "workspace_trash", **data}],
        )
