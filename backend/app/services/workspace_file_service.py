"""Safe, session-scoped filesystem primitives for Platform Tools."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from app.config import settings
from app.models.artifact_v1 import Artifact
from app.models.runtime_v1 import RunnerSkillSnapshot, Workspace


class WorkspaceFileError(ValueError):
    pass


def _relative_path(value: str, *, allow_dot: bool = False) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise WorkspaceFileError("path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise WorkspaceFileError("absolute paths and parent traversal are not allowed")
    if not allow_dot and (str(path) in {"", "."}):
        raise WorkspaceFileError("path must identify a file")
    return path


def _inside(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise WorkspaceFileError("path escapes the permitted root") from error
    return resolved


async def workspace_root(db, *, workspace_id: str | None, session_id: str) -> Path:
    if not workspace_id:
        raise WorkspaceFileError("an active Workspace lock is required")
    workspace = await db.scalar(select(Workspace).where(
        Workspace.id == workspace_id,
        Workspace.session_id == session_id,
        Workspace.status == "active",
    ))
    if workspace is None:
        raise WorkspaceFileError("Workspace is unavailable")
    root = (Path(settings.sandbox_root).resolve() / session_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


async def private_agent_root(
    db, *, workspace_id: str | None, session_id: str, agent_id: str,
) -> Path:
    root = await workspace_root(
        db, workspace_id=workspace_id, session_id=session_id,
    )
    if not isinstance(agent_id, str) or not agent_id or "/" in agent_id or "\\" in agent_id:
        raise WorkspaceFileError("a valid Agent identity is required for Workspace access")
    private = _inside(root, root / "agents" / agent_id)
    private.mkdir(parents=True, exist_ok=True)
    return private


async def resolve_workspace_path(
    db, *, workspace_id: str | None, session_id: str, relative_path: str,
    agent_id: str | None = None, allow_dot: bool = False,
) -> tuple[Path, Path, str]:
    root = (
        await private_agent_root(
            db, workspace_id=workspace_id, session_id=session_id, agent_id=agent_id,
        )
        if agent_id else
        await workspace_root(db, workspace_id=workspace_id, session_id=session_id)
    )
    relative = _relative_path(relative_path, allow_dot=allow_dot)
    if relative.parts and relative.parts[0] == ".myclaw":
        raise WorkspaceFileError("the .myclaw Workspace namespace is reserved by the platform")
    candidate = _inside(root, root.joinpath(*relative.parts))
    return root, candidate, relative.as_posix()


async def resolve_read_path(db, *, context, value: str) -> tuple[Path, str]:
    if value.startswith("skill://"):
        without_scheme = value.removeprefix("skill://")
        parsed = _relative_path(without_scheme)
        if len(parsed.parts) < 3:
            raise WorkspaceFileError("Skill URI must identify a file")
        skill_id, checksum, *tail = parsed.parts
        prefix = f"skill://{skill_id}/{checksum}/"
        snapshot = await db.scalar(select(RunnerSkillSnapshot).where(
            RunnerSkillSnapshot.runner_id == context.runner_id,
            RunnerSkillSnapshot.skill_id == skill_id,
            RunnerSkillSnapshot.checksum == checksum,
            RunnerSkillSnapshot.path_uri == prefix,
        ))
        if snapshot is None:
            raise WorkspaceFileError("Skill URI is not in this Runner's frozen catalog")
        root = (Path(settings.shared_skill_root) / skill_id / checksum).resolve()
        candidate = _inside(root, root.joinpath(*tail))
        return candidate, value
    _, candidate, relative = await resolve_workspace_path(
        db, workspace_id=context.workspace_id, session_id=context.session_id,
        relative_path=value, agent_id=context.agent_id,
    )
    return candidate, relative


async def resolve_artifact_storage_path(db, *, context, artifact_id: str) -> tuple[Path, Artifact]:
    artifact = await db.scalar(select(Artifact).where(
        Artifact.id == artifact_id,
        Artifact.user_id == context.user_id,
        Artifact.session_id == context.session_id,
        Artifact.workspace_id == context.workspace_id,
        Artifact.status == "published",
    ))
    if artifact is None:
        raise WorkspaceFileError("source Artifact is unavailable")
    root = await workspace_root(
        db, workspace_id=context.workspace_id, session_id=context.session_id,
    )
    relative = _relative_path(artifact.relative_path)
    if len(relative.parts) < 4 or relative.parts[:2] != (".myclaw", "artifacts"):
        raise WorkspaceFileError("source Artifact storage reference is invalid")
    path = _inside(root, root.joinpath(*relative.parts))
    if not path.is_file() or path.stat().st_size != artifact.size_bytes or sha256_file(path) != artifact.checksum:
        raise WorkspaceFileError("source Artifact bytes failed integrity validation")
    return path, artifact


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


SAFE_ENV_KEY = re.compile(r"^(LANG|LC_[A-Z_]+|TZ|TERM)$")


def sandbox_environment(root: Path, requested: dict | None) -> dict[str, str]:
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(root),
        "TMPDIR": str(root / ".tmp"),
        "LANG": "C.UTF-8",
    }
    (root / ".tmp").mkdir(parents=True, exist_ok=True)
    for key, value in (requested or {}).items():
        if not isinstance(key, str) or not SAFE_ENV_KEY.fullmatch(key):
            raise WorkspaceFileError(f"environment variable is not allowed: {key}")
        if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
            raise WorkspaceFileError(f"invalid environment value: {key}")
        environment[key] = value
    return environment
