from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.groups import owned_group
from app.api.sessions import _idempotency, _owned_session
from app.config import settings
from app.database import AsyncSessionLocal, get_db
from app.deps import SessionActor, get_session_actor
from app.models.artifact_v1 import Artifact, GroupArtifact
from app.models.common import utcnow, uuid7
from app.models.runtime_v1 import WorkspaceLockRequest
from app.persistence.reliable_writes import ExecutionEventWriter
from app.services.artifact_service import artifact_view
from app.services.cursor_service import SortKey, paginate_scalars
from app.services.workspace_file_service import sha256_file
from app.services.workspace_lock_service import (
    WorkspaceLockConflict, acquire_workspace_lock, release_workspace_lock,
)

router = APIRouter(tags=["artifacts"])


def _download_content_disposition(filename: str) -> str:
    """Build an RFC 6266 header with an ASCII fallback and UTF-8 filename*."""
    safe_name = filename.replace("\r", "").replace("\n", "")
    fallback = "".join(
        character for character in safe_name
        if character.isascii() and (character.isalnum() or character in {" ", ".", "_", "-"})
    ).strip()
    if not fallback or fallback.startswith("."):
        suffix = "".join(
            character for character in Path(safe_name).suffix
            if character.isascii() and (character.isalnum() or character in {".", "_", "-"})
        )
        fallback = f"artifact{suffix}"
    encoded = quote(safe_name, safe="!#$&+-.^_`|~")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def _artifact_path(artifact: Artifact) -> Path:
    relative = PurePosixPath(artifact.relative_path)
    if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 4
            or relative.parts[:2] != (".myclaw", "artifacts")
            or relative.parts[2] != artifact.id):
        raise HTTPException(status_code=410, detail={"code": "artifact_storage_invalid"})
    root = (Path(settings.sandbox_root) / artifact.session_id).resolve()
    target = root.joinpath(*relative.parts).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise HTTPException(status_code=410, detail={"code": "artifact_storage_invalid"}) from error
    return target


async def _owned_artifact(db: AsyncSession, artifact_id: str, actor: SessionActor, *, lock=False) -> Artifact:
    query = select(Artifact).where(
        Artifact.id == artifact_id,
        Artifact.tenant_id == actor.tenant_id,
    )
    if lock:
        query = query.with_for_update()
    artifact = await db.scalar(query)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    await _owned_session(db, artifact.session_id, actor)
    return artifact


async def _page(
    statement, db: AsyncSession, cursor: str | None, limit: int, *,
    actor: SessionActor, resource: str, filters: dict,
):
    rows, next_cursor = await paginate_scalars(
        db, statement,
        sort=(SortKey(Artifact.created_at), SortKey(Artifact.id)),
        limit=limit, cursor=cursor,
        principal=f"{actor.principal_type}:{actor.principal_id}",
        resource=resource, filters=filters,
    )
    return {"items": [artifact_view(row) for row in rows], "next_cursor": next_cursor}


@router.get("/sessions/{session_id}/artifacts")
async def list_session_artifacts(
    session_id: str, status: str | None = None, cursor: str | None = None, limit: int = 50,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    await _owned_session(db, session_id, actor)
    if status not in (None, "publishing", "published", "unavailable", "deleted"):
        raise HTTPException(status_code=422, detail={"code": "invalid_artifact_status"})
    statement = select(Artifact).where(
        Artifact.tenant_id == actor.tenant_id,
        Artifact.session_id == session_id,
    )
    if status:
        statement = statement.where(Artifact.status == status)
    return await _page(
        statement, db, cursor, limit, actor=actor,
        resource=f"session:{session_id}:artifacts", filters={"status": status},
    )


@router.get("/groups/{group_id}/artifacts")
async def list_group_artifacts(
    group_id: str, status: str | None = None, cursor: str | None = None, limit: int = 50,
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    await owned_group(db, group_id, actor)
    if status not in (None, "published", "unavailable", "deleted"):
        raise HTTPException(status_code=422, detail={"code": "invalid_artifact_status"})
    statement = select(Artifact).join(GroupArtifact, GroupArtifact.artifact_id == Artifact.id).where(
        Artifact.tenant_id == actor.tenant_id,
        GroupArtifact.group_session_id == group_id,
    )
    if status:
        statement = statement.where(Artifact.status == status)
    return await _page(
        statement, db, cursor, limit, actor=actor,
        resource=f"group:{group_id}:artifacts", filters={"status": status},
    )


@router.get("/artifacts/{artifact_id}")
async def get_artifact(
    artifact_id: str, db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    return artifact_view(await _owned_artifact(db, artifact_id, actor))


def _range(value: str | None, size: int) -> tuple[int, int, int]:
    if not value:
        return 0, size - 1, 200
    if not value.startswith("bytes=") or "," in value:
        raise HTTPException(status_code=416, detail={"code": "invalid_range"})
    raw = value.removeprefix("bytes=")
    try:
        start_raw, end_raw = raw.split("-", 1)
        if not start_raw:
            length = int(end_raw)
            if length <= 0:
                raise ValueError
            start, end = max(0, size - length), size - 1
        else:
            start = int(start_raw)
            end = min(int(end_raw), size - 1) if end_raw else size - 1
        if start < 0 or start >= size or end < start:
            raise ValueError
        return start, end, 206
    except ValueError as error:
        raise HTTPException(status_code=416, detail={"code": "range_not_satisfiable"}) from error


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: str, request: Request, db: AsyncSession = Depends(get_db),
    actor: SessionActor = Depends(get_session_actor),
):
    artifact = await _owned_artifact(db, artifact_id, actor, lock=True)
    if artifact.status != "published":
        raise HTTPException(status_code=410, detail={"code": f"artifact_{artifact.status}"})
    grant = await acquire_workspace_lock(
        db, workspace_id=artifact.workspace_id, tool_invocation_id=None,
        mode="read", lease_seconds=300,
    )
    if grant.status != "granted":
        queued = await db.get(WorkspaceLockRequest, grant.request_id)
        queued.status = "expired"; queued.released_at = utcnow(); queued.version += 1
        await db.commit()
        raise HTTPException(status_code=423, detail={"code": "workspace_busy"})
    target = _artifact_path(artifact)
    if not target.is_file() or target.stat().st_size != artifact.size_bytes or sha256_file(target) != artifact.checksum:
        artifact.status = "unavailable"; artifact.version += 1
        await ExecutionEventWriter(db).append(
            session_id=artifact.session_id, event_type="artifact.unavailable",
            event_dedupe_key=f"transition:artifact:{artifact.id}:unavailable:download",
            actor_type="service", actor_id="artifact-download", entity_type="artifact",
            entity_id=artifact.id, entity_version=artifact.version, visibility="user",
            summary="Artifact file is unavailable", payload={"artifact_id": artifact.id},
            correlation_id=artifact.id,
        )
        await release_workspace_lock(db, request_id=grant.request_id, lease_token=grant.lease_token)
        await db.commit()
        raise HTTPException(status_code=410, detail={"code": "artifact_unavailable"})
    start, end, response_status = _range(request.headers.get("range"), artifact.size_bytes)
    await db.commit()

    async def body():
        try:
            with target.open("rb") as handle:
                handle.seek(start)
                remaining = max(0, end - start + 1)
                while remaining:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
        finally:
            async with AsyncSessionLocal.begin() as release_db:
                try:
                    await release_workspace_lock(
                        release_db, request_id=grant.request_id, lease_token=grant.lease_token,
                    )
                except WorkspaceLockConflict:
                    pass

    headers = {
        "ETag": f'"{artifact.checksum}"', "Accept-Ranges": "bytes",
        "Content-Length": str(max(0, end - start + 1)),
        "Content-Disposition": _download_content_disposition(artifact.name),
    }
    if response_status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{artifact.size_bytes}"
    return StreamingResponse(body(), status_code=response_status, media_type=artifact.mime_type, headers=headers)


@router.post("/artifacts/{artifact_id}/delete")
async def delete_artifact(
    artifact_id: str, if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), actor: SessionActor = Depends(get_session_actor),
):
    record, replay = await _idempotency(
        db, actor=actor, operation="delete_artifact", key=idempotency_key,
        request={"artifact_id": artifact_id, "if_match": if_match},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    artifact = await _owned_artifact(db, artifact_id, actor, lock=True)
    raw = (if_match or "").replace("W/", "").strip(' "')
    if not raw.isdigit():
        raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    if int(raw) != artifact.version:
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": artifact.version})
    if artifact.status != "deleted":
        previous = artifact.version
        artifact.status = "deleted"; artifact.version += 1
        await ExecutionEventWriter(db).append(
            session_id=artifact.session_id, event_type="artifact.deleted",
            event_dedupe_key=f"transition:artifact:{artifact.id}:{previous}:{artifact.version}:delete",
            actor_type=actor.principal_type, actor_id=actor.principal_id,
            entity_type="artifact", entity_id=artifact.id, entity_version=artifact.version,
            visibility="user", summary="Artifact deleted",
            payload={"artifact_id": artifact.id}, correlation_id=artifact.id,
        )
    response = artifact_view(artifact)
    record.resource_type = "artifact"; record.resource_id = artifact.id
    record.http_status = 200; record.response_snapshot = response
    await db.commit()
    return response
