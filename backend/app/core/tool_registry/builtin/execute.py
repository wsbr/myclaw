from __future__ import annotations

import asyncio
import os
import signal

from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.services.workspace_file_service import (
    execution_workspace_root, resolve_workspace_path, sandbox_environment,
)


def _snapshot(root):
    values = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and len(values) < 10000:
            stat = path.stat()
            values[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return values


class ExecuteTool(BaseTool):
    name = "execute"
    description = "在当前 Local Sandbox 中以 argv（非 shell）执行进程，限制环境、输出与超时"
    input_schema = {
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 128},
            "cwd": {"type": "string", "default": "."},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 20, "default": 20},
            "env": {"type": "object", "additionalProperties": {"type": "string"}},
            "stdin": {"type": "string", "maxLength": 1048576},
        },
        "required": ["argv"],
        "additionalProperties": False,
    }

    async def execute(self, args: dict, context: RuntimeContext, db=None) -> ToolResult:
        if db is None:
            return ToolResult(success=False, error="database connection is required")
        argv = args.get("argv")
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
                or any(not isinstance(item, str) or not item or "\x00" in item or len(item) > 8192 for item in argv)):
            return ToolResult(success=False, error="argv must be an array of 1-128 non-empty strings")
        timeout = args.get("timeout_seconds", 20)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 20:
            return ToolResult(success=False, error="timeout_seconds must be between 1 and 20")
        stdin = args.get("stdin", "")
        if not isinstance(stdin, str) or len(stdin.encode("utf-8")) > 1024 * 1024:
            return ToolResult(success=False, error="stdin exceeds the 1 MiB limit")
        root = await execution_workspace_root(db, context=context)
        _, cwd, _ = await resolve_workspace_path(
            db, workspace_id=context.workspace_id, session_id=context.session_id,
            relative_path=args.get("cwd", "."), agent_id=context.agent_id,
            group_session_id=context.group_session_id,
            allow_dot=True,
        )
        if not cwd.is_dir():
            return ToolResult(success=False, error="cwd does not exist or is not a directory")
        environment = sandbox_environment(root, args.get("env"))
        before = _snapshot(root)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            return ToolResult(success=False, error=f"failed to start process: {error}")
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin.encode("utf-8")), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = await process.communicate()
        output_limit = 65536
        after = _snapshot(root)
        changed = sorted(path for path, value in after.items() if before.get(path) != value)[:200]
        data = {
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "stdout": stdout[:output_limit].decode("utf-8", errors="replace"),
            "stderr": stderr[:output_limit].decode("utf-8", errors="replace"),
            "stdout_truncated": len(stdout) > output_limit,
            "stderr_truncated": len(stderr) > output_limit,
            "output_files": changed,
        }
        if timed_out:
            return ToolResult(success=False, data=data, error="process timed out")
        return ToolResult(success=True, data=data)
