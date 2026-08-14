"""Phase-one account and platform administration."""

import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.database import get_db
from app.deps import CurrentAccount, get_current_admin
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    IdempotencyRecord, PlatformAccount, PlatformAccountRole, UserIdentity,
)
from app.models.runtime_v1 import Run
from app.models.tenant_v1 import Tenant, TenantMembership
from app.persistence.reliable_writes import PlatformAuditWriter
from app.services.cursor_service import SortKey, paginate_scalars


router = APIRouter(prefix="/admin", tags=["admin"])
VALID_ROLES = {"user", "builder", "admin"}


class AccountCreate(BaseModel):
    user_id: str | None = Field(default=None, min_length=1, max_length=191)
    email: str
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=500)
    roles: list[str] = Field(default_factory=lambda: ["user"], min_length=1, max_length=3)


class AccountRolesPatch(BaseModel):
    roles: list[str] = Field(min_length=1, max_length=3)


class AccountStatusPatch(BaseModel):
    status: str


def _expected(value: str | None) -> int:
    raw = (value or "").replace("W/", "").strip('" ')
    if not raw.isdigit():
        raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    return int(raw)


def _validate_roles(roles: list[str]) -> list[str]:
    values = sorted(set(roles))
    if "user" not in values or any(role not in VALID_ROLES for role in values):
        raise HTTPException(status_code=422, detail={
            "code": "invalid_roles", "message": "roles must include user and only user/builder/admin",
        })
    return values


async def _account_view(db: AsyncSession, account: PlatformAccount) -> dict:
    roles = sorted(await db.scalars(select(PlatformAccountRole.role_key).where(
        PlatformAccountRole.account_id == account.id,
    )))
    tenant = await db.get(Tenant, account.tenant_id)
    return {
        "id": account.id,
        "tenant_id": account.tenant_id,
        "tenant": ({
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.name,
            "status": tenant.status,
        } if tenant is not None else None),
        "user_id": account.user_id,
        "email": account.email,
        "display_name": account.display_name,
        "status": account.status,
        "roles": roles,
        "version": account.version,
        "auth_revision": account.auth_revision,
        "created_at": account.created_at.isoformat() if account.created_at else None,
        "updated_at": account.updated_at.isoformat() if account.updated_at else None,
    }


async def _idempotency(
    db: AsyncSession, current: CurrentAccount, operation: str, key: str, request: dict,
) -> tuple[IdempotencyRecord, bool]:
    digest = hashlib.sha256(json.dumps(
        request, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    record = await db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.principal_type == "account",
        IdempotencyRecord.principal_id == current.account.id,
        IdempotencyRecord.operation == operation,
        IdempotencyRecord.idempotency_key == key,
    ))
    if record is not None:
        if record.request_hash != digest:
            raise HTTPException(status_code=409, detail={"code": "idempotency_mismatch"})
        return record, True
    record = IdempotencyRecord(
        id=uuid7(), principal_type="account", principal_id=current.account.id,
        operation=operation, idempotency_key=key, request_hash=digest,
        expires_at=utcnow() + timedelta(days=7),
    )
    db.add(record)
    await db.flush()
    return record, False


@router.get("/accounts")
async def list_accounts(
    status: str | None = None, role: str | None = None, user_id_prefix: str | None = None,
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_admin),
):
    query = select(PlatformAccount).where(
        PlatformAccount.tenant_id == current.tenant_id,
    )
    if status:
        query = query.where(PlatformAccount.status == status)
    if user_id_prefix:
        query = query.where(PlatformAccount.user_id.like(f"{user_id_prefix}%"))
    if role:
        query = query.where(PlatformAccount.id.in_(select(PlatformAccountRole.account_id).where(
            PlatformAccountRole.role_key == role,
        )))
    filters = {"status": status, "role": role, "user_id_prefix": user_id_prefix}
    rows, next_cursor = await paginate_scalars(
        db, query, sort=(SortKey(PlatformAccount.created_at), SortKey(PlatformAccount.id)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource="admin:accounts", filters=filters,
    )
    return {"items": [await _account_view(db, row) for row in rows], "next_cursor": next_cursor}


@router.post("/accounts", status_code=201)
async def create_account(
    body: AccountCreate, idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_admin),
):
    roles = _validate_roles(body.roles)
    record, replay = await _idempotency(
        db, current, "create_platform_account", idempotency_key, body.model_dump(),
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    user_id = body.user_id or uuid7()
    if await db.get(UserIdentity, user_id) is not None:
        raise HTTPException(status_code=409, detail={"code": "user_id_already_exists"})
    if await db.scalar(select(PlatformAccount.id).where(PlatformAccount.email == body.email)):
        raise HTTPException(status_code=409, detail={"code": "email_already_exists"})
    account = PlatformAccount(
        id=uuid7(), tenant_id=current.tenant_id,
        user_id=user_id, email=body.email,
        display_name=body.display_name.strip(), password_hash=hash_password(body.password),
        status="active", auth_revision=1, version=1,
    )
    db.add(UserIdentity(user_id=user_id, status="active"))
    await db.flush()
    db.add(account)
    await db.flush()
    db.add_all([PlatformAccountRole(
        account_id=account.id, role_key=role, granted_by_account_id=current.account.id,
        granted_at=utcnow(),
    ) for role in roles])
    db.add(TenantMembership(
        id=uuid7(), tenant_id=current.tenant_id, account_id=account.id,
        role_key="admin" if "admin" in roles else "member",
        status="active", version=1,
    ))
    await db.flush()
    response = await _account_view(db, account)
    await PlatformAuditWriter(db).append(
        category="identity", action="account.created", actor_type="account",
        actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="platform_account", resource_id=account.id,
        resource_version=account.version, outcome="succeeded", summary="Platform account created",
        changes={"roles": roles, "status": account.status},
        dedupe_key=f"audit:create_platform_account:{record.id}",
    )
    record.resource_type = "platform_account"
    record.resource_id = account.id
    record.http_status = 201
    record.response_snapshot = response
    await db.commit()
    return response


@router.get("/accounts/{account_id}")
async def get_account(
    account_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_admin),
):
    account = await db.scalar(select(PlatformAccount).where(
        PlatformAccount.id == account_id,
        PlatformAccount.tenant_id == current.tenant_id,
    ))
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return await _account_view(db, account)


@router.patch("/accounts/{account_id}/roles")
async def patch_account_roles(
    account_id: str, body: AccountRolesPatch,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_admin),
):
    roles = _validate_roles(body.roles)
    record, replay = await _idempotency(
        db, current, "patch_platform_account_roles", idempotency_key,
        {"account_id": account_id, "roles": roles},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    account = await db.scalar(select(PlatformAccount).where(
        PlatformAccount.id == account_id,
        PlatformAccount.tenant_id == current.tenant_id,
    ).with_for_update())
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.version != _expected(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "version": account.version})
    if account.id == current.account.id and "admin" not in roles:
        raise HTTPException(status_code=409, detail={"code": "cannot_remove_own_admin_role"})
    before = sorted(await db.scalars(select(PlatformAccountRole.role_key).where(
        PlatformAccountRole.account_id == account.id,
    )))
    await db.execute(delete(PlatformAccountRole).where(PlatformAccountRole.account_id == account.id))
    db.add_all([PlatformAccountRole(
        account_id=account.id, role_key=role, granted_by_account_id=current.account.id,
        granted_at=utcnow(),
    ) for role in roles])
    membership = await db.scalar(select(TenantMembership).where(
        TenantMembership.tenant_id == current.tenant_id,
        TenantMembership.account_id == account.id,
    ).with_for_update())
    if membership is None:
        raise HTTPException(status_code=409, detail={"code": "tenant_membership_missing"})
    membership.role_key = "admin" if "admin" in roles else "member"
    membership.version += 1
    account.version += 1
    account.auth_revision += 1
    await db.flush()
    response = await _account_view(db, account)
    await PlatformAuditWriter(db).append(
        category="identity", action="account.roles_updated", actor_type="account",
        actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="platform_account", resource_id=account.id,
        resource_version=account.version, outcome="succeeded", summary="Platform account roles updated",
        changes={"roles": {"from": before, "to": roles}},
        dedupe_key=f"audit:patch_platform_account_roles:{record.id}",
    )
    record.resource_type, record.resource_id, record.http_status = "platform_account", account.id, 200
    record.response_snapshot = response
    await db.commit()
    return response


@router.patch("/accounts/{account_id}/status")
async def patch_account_status(
    account_id: str, body: AccountStatusPatch,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_admin),
):
    if body.status not in {"active", "disabled"}:
        raise HTTPException(status_code=422, detail={"code": "invalid_account_status"})
    record, replay = await _idempotency(
        db, current, "patch_platform_account_status", idempotency_key,
        {"account_id": account_id, "status": body.status},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    account = await db.scalar(select(PlatformAccount).where(
        PlatformAccount.id == account_id,
        PlatformAccount.tenant_id == current.tenant_id,
    ).with_for_update())
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.version != _expected(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "version": account.version})
    if account.id == current.account.id and body.status != "active":
        raise HTTPException(status_code=409, detail={"code": "cannot_disable_own_account"})
    before = account.status
    account.status = body.status
    account.version += 1
    account.auth_revision += 1
    await db.flush()
    response = await _account_view(db, account)
    await PlatformAuditWriter(db).append(
        category="identity", action="account.status_updated", actor_type="account",
        actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="platform_account", resource_id=account.id,
        resource_version=account.version, outcome="succeeded", summary="Platform account status updated",
        changes={"status": {"from": before, "to": body.status}},
        dedupe_key=f"audit:patch_platform_account_status:{record.id}",
    )
    record.resource_type, record.resource_id, record.http_status = "platform_account", account.id, 200
    record.response_snapshot = response
    await db.commit()
    return response


@router.get("/agents")
async def list_all_agents(
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_admin),
):
    agents, next_cursor = await paginate_scalars(
        db, select(AgentBlueprint).where(
            AgentBlueprint.tenant_id == current.tenant_id,
        ),
        sort=(SortKey(AgentBlueprint.created_at), SortKey(AgentBlueprint.id)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource="admin:agents", filters={},
    )
    items = []
    for agent in agents:
        version = await db.get(AgentAssemblyVersion, agent.current_published_version_id or agent.draft_version_id)
        items.append({
            "id": agent.id, "name": version.name if version else "",
            "status": agent.status, "owner_account_id": agent.owner_account_id,
            "visibility": agent.visibility, "group_enabled": version.group_enabled if version else False,
            "created_at": agent.created_at,
        })
    return {"items": items, "next_cursor": next_cursor}


@router.get("/stats")
async def global_stats(
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_admin),
):
    return {
        "total_users": await db.scalar(select(func.count(PlatformAccount.id)).where(
            PlatformAccount.tenant_id == current.tenant_id,
        )),
        "total_agents": await db.scalar(select(func.count(AgentBlueprint.id)).where(
            AgentBlueprint.tenant_id == current.tenant_id,
        )),
        "total_runs": await db.scalar(select(func.count(Run.id)).where(
            Run.tenant_id == current.tenant_id,
        )),
    }
