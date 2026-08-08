from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import errno
import os
import platform
import resource
import shutil
import signal
import subprocess
from pathlib import Path

from sqlalchemy import select

from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.database import AsyncSessionLocal
from app.models.runtime_v1 import ToolInvocation
from app.services.workspace_file_service import (
    execution_workspace_root, resolve_workspace_path, sandbox_environment,
    sha256_file,
)


LIBSECCOMP_NAME = ctypes.util.find_library("seccomp") or "libseccomp.so.2"
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
PRLIMIT_PATH = shutil.which("prlimit")


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]


def _snapshot(root):
    values = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and len(values) < 10000:
            stat = path.stat()
            values[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return values


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, int]:
    kept = bytearray()
    total = 0
    while chunk := await stream.read(65536):
        total += len(chunk)
        if len(kept) < limit:
            kept.extend(chunk[:limit - len(kept)])
    return bytes(kept), total


async def _feed_stdin(stream: asyncio.StreamWriter, value: bytes) -> None:
    try:
        if value:
            stream.write(value)
            await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()


def _deny_network_syscalls() -> None:
    """Install a fail-closed seccomp rule before executing user argv.

    The Sandbox Worker needs database connectivity for its control plane, so
    container networking alone cannot express the child process's ``egress=none``
    policy. All non-stdio descriptors are closed by ``subprocess_exec``; denying
    creation of new sockets therefore removes the remaining network path.
    """

    if platform.system() != "Linux":
        raise RuntimeError("execute network isolation requires Linux seccomp")
    library = ctypes.CDLL(LIBSECCOMP_NAME, use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
    ]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise OSError(ctypes.get_errno(), "seccomp_init failed")
    try:
        deny = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO(EPERM)
        for name in (
            b"socket", b"socketcall", b"ptrace", b"process_vm_readv",
            b"process_vm_writev", b"bpf", b"keyctl", b"add_key",
            b"request_key", b"mount", b"umount2", b"pivot_root",
            b"setns", b"unshare", b"userfaultfd",
        ):
            syscall = library.seccomp_syscall_resolve_name(name)
            if syscall < 0:
                continue
            result = library.seccomp_rule_add(context, deny, syscall, 0)
            if result != 0:
                raise OSError(-result, f"seccomp_rule_add failed for {name.decode()}")
        result = library.seccomp_load(context)
        if result != 0:
            raise OSError(-result, "seccomp_load failed")
    finally:
        library.seccomp_release(context)


def _landlock_restrict(workspace_root: str) -> None:
    """Confine filesystem access to system runtime files and one Workspace."""

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(
        _LANDLOCK_CREATE_RULESET, ctypes.c_void_p(), ctypes.c_size_t(0),
        ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    if abi < 1:
        error = ctypes.get_errno()
        raise OSError(error, "Landlock is unavailable")

    execute = 1 << 0
    write_file = 1 << 1
    read_file = 1 << 2
    read_dir = 1 << 3
    remove_dir = 1 << 4
    remove_file = 1 << 5
    make_char = 1 << 6
    make_dir = 1 << 7
    make_reg = 1 << 8
    make_sock = 1 << 9
    make_fifo = 1 << 10
    make_block = 1 << 11
    make_sym = 1 << 12
    handled = (
        execute | write_file | read_file | read_dir | remove_dir | remove_file
        | make_char | make_dir | make_reg | make_sock | make_fifo | make_block
        | make_sym
    )
    if abi >= 2:
        handled |= 1 << 13  # REFER
    if abi >= 3:
        handled |= 1 << 14  # TRUNCATE
    if abi >= 5:
        handled |= 1 << 15  # IOCTL_DEV
    ruleset_attr = _LandlockRulesetAttr(handled_access_fs=handled)
    ruleset_fd = libc.syscall(
        _LANDLOCK_CREATE_RULESET, ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr), ctypes.c_uint(0),
    )
    if ruleset_fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, "Landlock ruleset creation failed")

    opened: list[int] = []
    try:
        readonly = execute | read_file | read_dir
        writable = handled & ~(make_char | make_block)
        allowed_paths = [
            (workspace_root, writable),
            ("/usr", readonly), ("/bin", readonly),
            ("/lib", readonly), ("/lib64", readonly),
            ("/etc/ld.so.cache", read_file),
            ("/etc/localtime", read_file),
            ("/dev/null", read_file | write_file),
            ("/dev/urandom", read_file),
        ]
        open_flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC
        for value, access in allowed_paths:
            if not Path(value).exists():
                continue
            parent_fd = os.open(value, open_flags)
            opened.append(parent_fd)
            rule = _LandlockPathBeneathAttr(
                allowed_access=access, parent_fd=parent_fd, reserved=0,
            )
            result = libc.syscall(
                _LANDLOCK_ADD_RULE, ruleset_fd, _LANDLOCK_RULE_PATH_BENEATH,
                ctypes.byref(rule), ctypes.c_uint(0),
            )
            if result != 0:
                error = ctypes.get_errno()
                raise OSError(error, f"Landlock rule failed for {value}")
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, "PR_SET_NO_NEW_PRIVS failed")
        if libc.syscall(
            _LANDLOCK_RESTRICT_SELF, ruleset_fd, ctypes.c_uint(0),
        ) != 0:
            error = ctypes.get_errno()
            raise OSError(error, "Landlock restriction failed")
    finally:
        for descriptor in opened:
            os.close(descriptor)
        os.close(ruleset_fd)


def _resource_limits(*, cpu_seconds: int, workspace_root: str) -> None:
    """Install limits that are safe to apply in the forked Python child.

    RLIMIT_AS cannot be lowered here: the child temporarily retains the
    long-lived Worker's virtual address space until exec(2), which can already
    exceed the requested command limit.  The prlimit wrapper below applies the
    address-space limit after that Python image has been replaced.
    """

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
    if hasattr(resource, "RLIMIT_NPROC"):
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    _landlock_restrict(workspace_root)
    _deny_network_syscalls()


def _bounded_execution_argv(argv: list[str], *, memory_mb: int) -> list[str]:
    if not PRLIMIT_PATH:
        raise RuntimeError("prlimit is required for fail-closed memory isolation")
    memory_bytes = memory_mb * 1024 * 1024
    return [PRLIMIT_PATH, f"--as={memory_bytes}", "--", *argv]


async def _watch_cancellation(invocation_id: str) -> bool:
    while True:
        await asyncio.sleep(0.25)
        async with AsyncSessionLocal() as db:
            status = await db.scalar(select(ToolInvocation.status).where(
                ToolInvocation.id == invocation_id,
            ))
        if status in {None, "cancelled", "cancel_requested"}:
            return True


class ExecuteTool(BaseTool):
    name = "execute"
    description = (
        "WHEN: run a non-interactive process with explicit argv inside the current isolated Workspace. "
        "WHEN NOT: never pass shell syntax or use it for Browser/network actions. "
        "RETURNS: exit status, bounded stdout/stderr, consumed checksums, and changed-file evidence. "
        "RECOVERY: narrow argv/timeout and inspect stderr; timed-out process trees are killed."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 128},
            "cwd": {"type": "string", "default": "."},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 20, "default": 20},
            "cpu_seconds": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            "memory_mb": {"type": "integer", "minimum": 32, "maximum": 1024, "default": 256},
            "max_output_bytes": {"type": "integer", "minimum": 1024, "maximum": 1048576, "default": 65536},
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
        secret_markers = ("password", "secret", "token", "api-key", "api_key", "authorization", "credential")
        if any(
            item.startswith("-") and any(marker in item.casefold() for marker in secret_markers)
            for item in argv
        ):
            return ToolResult(
                status="failed",
                summary="Secret-bearing command-line arguments are prohibited",
                error="Use an administrator-managed Tool connection instead of passing secrets to execute",
                error_code="execute_secret_argument_prohibited",
                error_category="authorization",
            )
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
        cpu_seconds = int(args.get("cpu_seconds", min(timeout, 10)))
        memory_mb = int(args.get("memory_mb", 256))
        output_limit = int(args.get("max_output_bytes", 65536))
        input_file_checksums = {}
        for value in argv[1:]:
            if value.startswith("-"):
                continue
            candidate = (cwd / value).resolve(strict=False)
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                continue
            if candidate.is_file() and not candidate.is_symlink():
                input_file_checksums[value] = sha256_file(candidate)
        before = _snapshot(root)
        try:
            bounded_argv = _bounded_execution_argv(argv, memory_mb=memory_mb)
            process = await asyncio.create_subprocess_exec(
                *bounded_argv, cwd=cwd, env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                preexec_fn=lambda: _resource_limits(
                    cpu_seconds=cpu_seconds, workspace_root=str(root),
                ),
            )
        except (RuntimeError, subprocess.SubprocessError):
            return ToolResult(
                status="failed",
                summary="Sandbox filesystem/network isolation is unavailable",
                error="The process was not started because fail-closed isolation could not be installed",
                error_code="execute_isolation_unavailable",
                error_category="authorization",
                error_details={"side_effect_started": False},
            )
        except OSError as error:
            return ToolResult(
                status="failed", summary="Process could not be started",
                error=f"failed to start process: {error}",
                error_code="execute_start_failed", error_category="provider",
                error_details={"side_effect_started": False},
            )
        stdout_task = asyncio.create_task(_read_bounded(process.stdout, output_limit))
        stderr_task = asyncio.create_task(_read_bounded(process.stderr, output_limit))
        stdin_task = asyncio.create_task(_feed_stdin(process.stdin, stdin.encode("utf-8")))
        wait_task = asyncio.create_task(process.wait())
        cancel_task = (
            asyncio.create_task(_watch_cancellation(context.tool_invocation_id))
            if context.tool_invocation_id else None
        )
        timed_out = False
        cancelled = False
        wait_set = {wait_task, *((cancel_task,) if cancel_task else ())}
        done, _ = await asyncio.wait(
            wait_set, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )
        if wait_task not in done:
            cancelled = bool(cancel_task and cancel_task in done and cancel_task.result())
            timed_out = not cancelled
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await wait_task
        if cancel_task:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        await asyncio.gather(stdin_task, return_exceptions=True)
        (stdout, stdout_size), (stderr, stderr_size) = await asyncio.gather(
            stdout_task, stderr_task,
        )
        after = _snapshot(root)
        changed = sorted(path for path, value in after.items() if before.get(path) != value)[:200]
        data = {
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "stdout": stdout[:output_limit].decode("utf-8", errors="replace"),
            "stderr": stderr[:output_limit].decode("utf-8", errors="replace"),
            "stdout_truncated": stdout_size > output_limit,
            "stderr_truncated": stderr_size > output_limit,
            "stdout_bytes": stdout_size,
            "stderr_bytes": stderr_size,
            "output_files": changed,
            # These checksums bind the observed process result to the exact
            # Workspace bytes it consumed. They are execution evidence, not a
            # claim inferred later from a similarly named Artifact.
            "input_file_checksums": input_file_checksums,
            "network_policy": "landlock_workspace_only+seccomp_socket_denied",
        }
        if timed_out:
            return ToolResult(
                status="failed", summary="Process timed out and its process tree was killed",
                data=data, error="process timed out", error_code="process_timeout",
                error_category="timeout", retryable=False,
                evidence=[{"kind": "process", **data}],
            )
        if cancelled:
            return ToolResult(
                status="cancelled", summary="Process tree was killed after Run cancellation",
                data=data, error="Process cancelled", error_code="process_cancelled",
                error_category="cancelled", retryable=False,
                evidence=[{"kind": "process", **data}],
            )
        return ToolResult(
            status="succeeded", summary=f"Process exited with code {process.returncode}",
            data=data, content_blocks=[{"type": "json", "value": data}],
            evidence=[{"kind": "process", **data}],
        )
