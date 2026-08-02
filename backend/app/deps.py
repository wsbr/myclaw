"""Authentication and execution-principal dependencies."""

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.database import get_db
from app.models.foundation import AgentApiKey, PlatformAccount, PlatformAccountRole
from app.services.agent_api_key_service import authenticate_api_key


bearer_scheme = HTTPBearer()
optional_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentAccount:
    account: PlatformAccount
    roles: frozenset[str]

    def has_any_role(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))


@dataclass(frozen=True)
class SessionActor:
    account: PlatformAccount | None = None
    roles: frozenset[str] = frozenset()
    api_key: AgentApiKey | None = None

    @property
    def principal_type(self) -> str:
        return "agent_api_key" if self.api_key else "account"

    @property
    def principal_id(self) -> str:
        return self.api_key.id if self.api_key else self.account.id


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
    db: AsyncSession = Depends(get_db),
) -> SessionActor:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    token = credentials.credentials
    if token.startswith("myclaw_"):
        key = await authenticate_api_key(db, token)
        if key is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Agent API key")
        return SessionActor(api_key=key)
    current = await _resolve_account(token, db)
    return SessionActor(account=current.account, roles=current.roles)


async def get_agent_api_key(actor: SessionActor = Depends(get_session_actor)) -> AgentApiKey:
    if actor.api_key is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent API key required")
    return actor.api_key
