"""Authentication and execution-principal dependencies."""

from dataclasses import dataclass
import re

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.database import get_db
from app.models.foundation import AgentApiKey, PlatformAccount, PlatformAccountRole
from app.models.tenant_v1 import Tenant, TenantMembership
from app.services.agent_api_key_service import authenticate_api_key


bearer_scheme = HTTPBearer()
optional_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentAccount:
    account: PlatformAccount
    roles: frozenset[str]

    @property
    def tenant_id(self) -> str:
        return self.account.tenant_id

    def has_any_role(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))


@dataclass(frozen=True)
class SessionActor:
    account: PlatformAccount | None = None
    roles: frozenset[str] = frozenset()
    api_key: AgentApiKey | None = None
    asserted_user_id: str | None = None

    @property
    def tenant_id(self) -> str | None:
        if self.account is not None:
            return self.account.tenant_id
        if self.api_key is not None:
            return self.api_key.tenant_id
        return None

    @property
    def principal_type(self) -> str:
        return "agent_api_key" if self.api_key else "account"

    @property
    def principal_id(self) -> str:
        if self.api_key:
            represented = self.account.id if self.account else self.asserted_user_id
            return f"{self.api_key.id}:{represented}"
        return self.account.id


RAW_USER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,190}$")


async def _resolve_account(token: str, db: AsyncSession) -> CurrentAccount:
    payload = decode_token(token)
    account_id = payload.get("account_id") or payload.get("sub")
    account = await db.get(PlatformAccount, account_id) if account_id else None
    if account is None or account.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Platform account unavailable")
    if payload.get("auth_revision") != account.auth_revision:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token authorization is stale")
    if payload.get("user_id") != account.user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token identity is stale")
    if payload.get("tenant_id") != account.tenant_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token Tenant is stale")
    tenant = await db.get(Tenant, account.tenant_id)
    membership = await db.scalar(select(TenantMembership).where(
        TenantMembership.tenant_id == account.tenant_id,
        TenantMembership.account_id == account.id,
        TenantMembership.status == "active",
    ))
    if tenant is None or tenant.status != "active" or membership is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tenant authority unavailable")
    roles = frozenset(await db.scalars(select(PlatformAccountRole.role_key).where(
        PlatformAccountRole.account_id == account.id,
    )))
    return CurrentAccount(account=account, roles=roles)


async def get_current_account(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentAccount:
    return await _resolve_account(credentials.credentials, db)


async def get_current_builder(
    current: CurrentAccount = Depends(get_current_account),
) -> CurrentAccount:
    if not current.has_any_role("builder", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Builder required")
    return current


async def get_current_admin(
    current: CurrentAccount = Depends(get_current_account),
) -> CurrentAccount:
    if not current.has_any_role("admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return current


async def get_session_actor(
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_bearer_scheme),
    raw_user_id: str | None = Header(default=None, alias="X-MyClaw-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> SessionActor:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    token = credentials.credentials
    if token.startswith("myclaw_"):
        key = await authenticate_api_key(db, token)
        if key is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Agent API key")
        asserted = (raw_user_id or "").strip()
        if not asserted:
            raise HTTPException(status_code=400, detail={"code": "user_id_required"})
        if not RAW_USER_ID.fullmatch(asserted):
            raise HTTPException(status_code=400, detail={"code": "invalid_user_id"})
        account = await db.scalar(select(PlatformAccount).where(
            PlatformAccount.user_id == asserted,
            PlatformAccount.tenant_id == key.tenant_id,
            PlatformAccount.status == "active",
        ))
        return SessionActor(
            account=account, api_key=key, asserted_user_id=asserted,
        )
    if raw_user_id is not None:
        raise HTTPException(status_code=400, detail={"code": "invalid_identity_profile"})
    current = await _resolve_account(token, db)
    return SessionActor(account=current.account, roles=current.roles)


async def get_agent_api_key(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> AgentApiKey:
    key = await authenticate_api_key(db, credentials.credentials)
    if key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Agent API key")
    return key
