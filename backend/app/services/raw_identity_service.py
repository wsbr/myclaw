"""UAT raw-user identity resolution shared by Agent API entry paths."""

from __future__ import annotations

from sqlalchemy import select

from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, PlatformAccountRole, UserIdentity
from app.models.tenant_v1 import TenantMembership


async def ensure_raw_user_account(
    db, user_id: str, *, tenant_id: str,
) -> tuple[PlatformAccount, bool]:
    identity = await db.scalar(select(UserIdentity).where(
        UserIdentity.user_id == user_id,
    ).with_for_update())
    if identity is None:
        identity = UserIdentity(user_id=user_id, status="active")
        db.add(identity)
        await db.flush()
    elif identity.status != "active":
        raise ValueError("identity_unavailable")
    account = await db.scalar(select(PlatformAccount).where(
        PlatformAccount.user_id == user_id,
        PlatformAccount.tenant_id == tenant_id,
    ).with_for_update())
    created = account is None
    if account is None:
        account = PlatformAccount(
            id=uuid7(), tenant_id=tenant_id, user_id=user_id, display_name=user_id,
            password_hash=None, status="active", auth_revision=1,
        )
        db.add(account)
        await db.flush()
        db.add(PlatformAccountRole(
            account_id=account.id, role_key="user",
            granted_by_account_id=None, granted_at=utcnow(),
        ))
        db.add(TenantMembership(
            id=uuid7(), tenant_id=tenant_id, account_id=account.id,
            role_key="member", status="active", version=1,
        ))
    elif account.status != "active":
        raise ValueError("identity_unavailable")
    return account, created
