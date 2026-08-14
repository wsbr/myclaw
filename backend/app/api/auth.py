"""PlatformAccount authentication endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, verify_password
from app.database import get_db
from app.deps import CurrentAccount, _resolve_account, get_current_account
from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, PlatformAccountRole, UserIdentity
from app.models.tenant_v1 import DEFAULT_TENANT_ID, Tenant, TenantMembership
from app.persistence.reliable_writes import PlatformAuditWriter
from app.schemas.auth import LoginRequest, LoginResponse, UserResponse
from app.config import settings
from app.core.enterprise_identity import (
    EnterpriseIdentityUnavailable, InvalidEnterpriseAssertion, verify_enterprise_assertion,
)


router = APIRouter(prefix="/auth", tags=["auth"])


class RefreshRequest(BaseModel):
    access_token: str


async def _roles(db: AsyncSession, account_id: str) -> frozenset[str]:
    return frozenset(await db.scalars(select(PlatformAccountRole.role_key).where(
        PlatformAccountRole.account_id == account_id,
    )))


async def _require_active_tenant(db: AsyncSession, account: PlatformAccount) -> None:
    tenant = await db.get(Tenant, account.tenant_id)
    membership = await db.scalar(select(TenantMembership.id).where(
        TenantMembership.tenant_id == account.tenant_id,
        TenantMembership.account_id == account.id,
        TenantMembership.status == "active",
    ))
    if tenant is None or tenant.status != "active" or membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant authority unavailable",
        )


def account_response(account: PlatformAccount, roles: frozenset[str]) -> UserResponse:
    primary_role = "admin" if "admin" in roles else "builder" if "builder" in roles else "user"
    return UserResponse(
        id=account.id,
        tenant_id=account.tenant_id,
        user_id=account.user_id,
        email=account.email,
        username=account.display_name,
        role=primary_role,
        roles=sorted(roles),
        is_active=account.status == "active",
        status=account.status,
        version=account.version,
    )


def token_for(account: PlatformAccount, roles: frozenset[str]) -> str:
    return create_access_token(account.id, claims={
        "account_id": account.id,
        "tenant_id": account.tenant_id,
        "user_id": account.user_id,
        "auth_revision": account.auth_revision,
        "roles": sorted(roles),
    })


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    account = await db.scalar(select(PlatformAccount).where(PlatformAccount.email == body.email))
    if account is None or not account.password_hash or not verify_password(body.password, account.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if account.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")
    await _require_active_tenant(db, account)
    roles = await _roles(db, account.id)
    await PlatformAuditWriter(db).append(
        category="security", action="account.login", actor_type="account",
        actor_id=account.id, actor_user_id=account.user_id,
        resource_type="platform_account", resource_id=account.id,
        resource_version=account.version, outcome="succeeded", summary="Account logged in",
        dedupe_key=f"audit:login:{uuid7()}",
    )
    await db.commit()
    return LoginResponse(access_token=token_for(account, roles), user=account_response(account, roles))


@router.get("/me", response_model=UserResponse)
async def me(current: CurrentAccount = Depends(get_current_account)):
    return account_response(current.account, current.roles)


@router.post("/logout", status_code=204)
async def logout():
    return None


@router.post("/refresh", response_model=LoginResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    current = await _resolve_account(body.access_token, db)
    return LoginResponse(
        access_token=token_for(current.account, current.roles),
        user=account_response(current.account, current.roles),
    )


@router.get("/enterprise/start")
async def enterprise_start():
    if not settings.enterprise_login_url or not settings.enterprise_assertion_secret:
        raise HTTPException(status_code=503, detail={"code": "enterprise_identity_unavailable"})
    return RedirectResponse(settings.enterprise_login_url, status_code=307)


@router.get("/enterprise/callback", response_model=LoginResponse)
async def enterprise_callback(
    assertion: str = Query(min_length=20), db: AsyncSession = Depends(get_db),
):
    try:
        claims = verify_enterprise_assertion(assertion, audience="myclaw-enterprise-login")
    except EnterpriseIdentityUnavailable:
        raise HTTPException(status_code=503, detail={"code": "enterprise_identity_unavailable"})
    except InvalidEnterpriseAssertion:
        raise HTTPException(status_code=401, detail={"code": "invalid_enterprise_assertion"})
    user_id = claims["user_id"]
    account = await db.scalar(select(PlatformAccount).where(PlatformAccount.user_id == user_id))
    if account is None:
        identity = await db.get(UserIdentity, user_id)
        if identity is None:
            db.add(UserIdentity(user_id=user_id, status="active"))
            await db.flush()
        elif identity.status != "active":
            raise HTTPException(status_code=409, detail={"code": "identity_unavailable"})
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        if email and await db.scalar(select(PlatformAccount.id).where(PlatformAccount.email == email)):
            email = None
        account = PlatformAccount(
            id=uuid7(), tenant_id=DEFAULT_TENANT_ID, user_id=user_id, email=email,
            display_name=str(claims.get("name") or user_id)[:200], password_hash=None,
            status="active", auth_revision=1, version=1,
        )
        db.add(account)
        await db.flush()
        db.add(PlatformAccountRole(
            account_id=account.id, role_key="user", granted_by_account_id=None,
            granted_at=utcnow(),
        ))
        db.add(TenantMembership(
            id=uuid7(), tenant_id=account.tenant_id, account_id=account.id,
            role_key="member", status="active", version=1,
        ))
    if account.status != "active":
        raise HTTPException(status_code=403, detail="Account disabled")
    await _require_active_tenant(db, account)
    roles = await _roles(db, account.id)
    await PlatformAuditWriter(db).append(
        category="security", action="account.enterprise_login", actor_type="account",
        actor_id=account.id, actor_user_id=account.user_id,
        resource_type="platform_account", resource_id=account.id,
        resource_version=account.version, outcome="succeeded",
        summary="Account logged in through enterprise identity",
        dedupe_key=f"audit:enterprise_login:{uuid7()}",
    )
    await db.commit()
    return LoginResponse(access_token=token_for(account, roles), user=account_response(account, roles))
