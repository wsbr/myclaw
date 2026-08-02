"""Fair, leased, database-backed Workspace read/write lock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import utcnow, uuid7
from app.models.runtime_v1 import (
    Workspace, WorkspaceLock, WorkspaceLockRequest, WorkspaceReadLease,
)


class WorkspaceLockConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceLockGrant:
    request_id: str
    status: str
    mode: str
    ticket: int
    lease_token: str | None
    fencing_token: int | None


def _view(request: WorkspaceLockRequest) -> WorkspaceLockGrant:
    return WorkspaceLockGrant(
        request_id=request.id, status=request.status, mode=request.mode, ticket=request.ticket,
        lease_token=request.lease_token, fencing_token=request.fencing_token,
    )


async def _expire_stale(
    db: AsyncSession, state: WorkspaceLock, *, now,
) -> None:
    changed = False
    if state.writer_request_id and state.writer_lease_expires_at and state.writer_lease_expires_at <= now:
        request = await db.get(WorkspaceLockRequest, state.writer_request_id)
        if request is not None and request.status == "granted":
            request.status = "expired"
            request.released_at = now
            request.version += 1
        state.writer_request_id = None
        state.writer_lease_expires_at = None
        changed = True
    expired_reads = list(await db.scalars(select(WorkspaceReadLease).where(
        WorkspaceReadLease.workspace_id == state.workspace_id,
        WorkspaceReadLease.expires_at <= now,
    ).with_for_update()))
    for lease in expired_reads:
        request = await db.get(WorkspaceLockRequest, lease.request_id)
        if request is not None and request.status == "granted":
            request.status = "expired"
            request.released_at = now
            request.version += 1
        await db.delete(lease)
        changed = True
    if changed:
        state.version += 1
        await db.flush()


async def _grant_waiters(
    db: AsyncSession, state: WorkspaceLock, *, now, lease_seconds: int,
) -> list[WorkspaceLockRequest]:
    if state.writer_request_id is not None:
        return []
    active_reader = await db.scalar(select(WorkspaceReadLease.id).where(
        WorkspaceReadLease.workspace_id == state.workspace_id,
        WorkspaceReadLease.expires_at > now,
    ).limit(1))
    if active_reader is not None:
        return []
    queued = list(await db.scalars(select(WorkspaceLockRequest).where(
        WorkspaceLockRequest.workspace_id == state.workspace_id,
        WorkspaceLockRequest.status == "queued",
    ).order_by(WorkspaceLockRequest.ticket).with_for_update()))
    if not queued:
        return []
    selected = [queued[0]] if queued[0].mode == "write" else []
    if queued[0].mode == "read":
        for request in queued:
            if request.mode == "write":
                break
            selected.append(request)
    expiry = now + timedelta(seconds=lease_seconds)
    for request in selected:
        request.status = "granted"
        request.lease_token = uuid7()
        request.lease_expires_at = expiry
        request.granted_at = now
        request.version += 1
        if request.mode == "write":
            state.writer_fencing_token += 1
            state.writer_request_id = request.id
            state.writer_lease_expires_at = expiry
            request.fencing_token = state.writer_fencing_token
        else:
            db.add(WorkspaceReadLease(
                id=uuid7(), workspace_id=state.workspace_id, request_id=request.id,
                ticket=request.ticket, lease_token=request.lease_token, expires_at=expiry,
            ))
    state.version += 1
    await db.flush()
    return selected


async def acquire_workspace_lock(
    db: AsyncSession,
    *,
    workspace_id: str,
    tool_invocation_id: str | None,
    mode: str,
    lease_seconds: int = 30,
    request_id: str | None = None,
) -> WorkspaceLockGrant:
    if mode not in {"read", "write"}:
        raise ValueError("Workspace lock mode must be read or write")
    if lease_seconds < 1:
        raise ValueError("Workspace lock lease must be positive")
    workspace = await db.scalar(select(Workspace).where(
        Workspace.id == workspace_id,
    ).with_for_update())
    if workspace is None or workspace.status != "active":
        raise WorkspaceLockConflict("Workspace is unavailable")
    state = await db.scalar(select(WorkspaceLock).where(
        WorkspaceLock.workspace_id == workspace_id,
    ).with_for_update())
    if state is None:
        state = WorkspaceLock(
            workspace_id=workspace_id, writer_fencing_token=0, next_ticket=1,
        )
        db.add(state)
        await db.flush()
    now = utcnow()
    await _expire_stale(db, state, now=now)
    if request_id:
        existing = await db.get(WorkspaceLockRequest, request_id)
        if existing is not None:
            if (
                existing.workspace_id != workspace_id
                or existing.tool_invocation_id != tool_invocation_id
                or existing.mode != mode
            ):
                raise WorkspaceLockConflict("Workspace lock request idempotency collision")
            return _view(existing)
    request = WorkspaceLockRequest(
        id=request_id or uuid7(), workspace_id=workspace_id,
        tool_invocation_id=tool_invocation_id, mode=mode, status="queued",
        ticket=state.next_ticket,
    )
    state.next_ticket += 1
    state.version += 1
    db.add(request)
    await db.flush()
    await _grant_waiters(db, state, now=now, lease_seconds=lease_seconds)
    return _view(request)


async def release_workspace_lock(
    db: AsyncSession,
    *,
    request_id: str,
    lease_token: str,
    fencing_token: int | None = None,
    lease_seconds: int = 30,
) -> tuple[WorkspaceLockGrant, list[WorkspaceLockGrant]]:
    request_view = await db.get(WorkspaceLockRequest, request_id)
    if request_view is None:
        raise WorkspaceLockConflict("Workspace lock request not found")
    await db.scalar(select(Workspace).where(
        Workspace.id == request_view.workspace_id,
    ).with_for_update())
    state = await db.scalar(select(WorkspaceLock).where(
        WorkspaceLock.workspace_id == request_view.workspace_id,
    ).with_for_update())
    request = await db.scalar(select(WorkspaceLockRequest).where(
        WorkspaceLockRequest.id == request_id,
    ).with_for_update())
    now = utcnow()
    await _expire_stale(db, state, now=now)
    if request.status != "granted":
        raise WorkspaceLockConflict(f"Workspace lock is {request.status}")
    if request.lease_token != lease_token:
        raise WorkspaceLockConflict("Workspace lease token is stale")
    if request.mode == "write":
        if request.fencing_token != fencing_token or state.writer_request_id != request.id:
            raise WorkspaceLockConflict("Workspace writer fencing token is stale")
        state.writer_request_id = None
        state.writer_lease_expires_at = None
    else:
        lease = await db.scalar(select(WorkspaceReadLease).where(
            WorkspaceReadLease.request_id == request.id,
            WorkspaceReadLease.lease_token == lease_token,
        ).with_for_update())
        if lease is None:
            raise WorkspaceLockConflict("Workspace read lease is stale")
        await db.delete(lease)
    request.status = "released"
    request.released_at = now
    request.lease_expires_at = None
    request.version += 1
    state.version += 1
    await db.flush()
    granted = await _grant_waiters(db, state, now=now, lease_seconds=lease_seconds)
    return _view(request), [_view(item) for item in granted]


async def assert_workspace_lock(
    db: AsyncSession,
    *,
    request_id: str,
    lease_token: str,
    fencing_token: int | None = None,
) -> WorkspaceLockRequest:
    request = await db.get(WorkspaceLockRequest, request_id)
    if (
        request is None or request.status != "granted"
        or request.lease_token != lease_token
        or request.lease_expires_at is None or request.lease_expires_at <= utcnow()
    ):
        raise WorkspaceLockConflict("Workspace lease is not current")
    if request.mode == "write" and request.fencing_token != fencing_token:
        raise WorkspaceLockConflict("Workspace writer fencing token is stale")
    return request


async def recover_workspace_lock(
    db: AsyncSession, *, workspace_id: str, lease_seconds: int = 30,
) -> list[WorkspaceLockGrant]:
    workspace = await db.scalar(select(Workspace).where(
        Workspace.id == workspace_id,
    ).with_for_update())
    if workspace is None:
        return []
    state = await db.scalar(select(WorkspaceLock).where(
        WorkspaceLock.workspace_id == workspace_id,
    ).with_for_update())
    if state is None:
        return []
    now = utcnow()
    await _expire_stale(db, state, now=now)
    granted = await _grant_waiters(db, state, now=now, lease_seconds=lease_seconds)
    return [_view(item) for item in granted]
