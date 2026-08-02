"""Self-service PlatformAccount profile and canonical userId endpoints."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import _account_view
from app.core.enterprise_identity import (
    EnterpriseIdentityUnavailable, InvalidEnterpriseAssertion, verify_enterprise_assertion,
)
from app.database import get_db
from app.deps import CurrentAccount, get_current_account
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord, PlatformAccount, UserIdentity
from app.persistence.reliable_writes import PlatformAuditWriter


router = APIRouter(prefix="/accounts/me", tags=["accounts"])


class AccountProfilePatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=255)


class ChangeUserIdRequest(BaseModel):
    enterprise_assertion: str = Field(min_length=20)


def _expected(value: str | None) -> int:
    raw = (value or "").replace("W/", "").strip('" ')
    if not raw.isdigit():
        raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    return int(raw)


async def _idem(
    db: AsyncSession, current: CurrentAccount, operation: str, key: str, request: dict,
) -> tuple[IdempotencyRecord, bool]:
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    record = await db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.principal_type == "account",
        IdempotencyRecord.principal_id == current.account.id,
        IdempotencyRecord.operation == operation,
        IdempotencyRecord.idempotency_key == key,
    ))
    if record:
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


@router.get("")
async def get_my_account(
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    return await _account_view(db, current.account)


@router.patch("")
async def patch_my_account(
    body: AccountProfilePatch,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail={"code": "empty_patch"})
    record, replay = await _idem(db, current, "patch_my_account", idempotency_key, changes)
    if replay and record.response_snapshot:
        return record.response_snapshot
    account = await db.scalar(select(PlatformAccount).where(
        PlatformAccount.id == current.account.id,
    ).with_for_update())
    if account.version != _expected(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "version": account.version})
    if "email" in changes and changes["email"]:
        duplicate = await db.scalar(select(PlatformAccount.id).where(
            PlatformAccount.email == changes["email"], PlatformAccount.id != account.id,
        ))
        if duplicate:
            raise HTTPException(status_code=409, detail={"code": "email_already_exists"})
    before = {key: getattr(account, key) for key in changes}
    for field, value in changes.items():
        setattr(account, field, value.strip() if isinstance(value, str) else value)
    account.version += 1
    if "email" in changes:
        account.auth_revision += 1
    await db.flush()
    response = await _account_view(db, account)
    await PlatformAuditWriter(db).append(
        category="identity", action="account.profile_updated", actor_type="account",
        actor_id=account.id, actor_user_id=account.user_id,
        resource_type="platform_account", resource_id=account.id,
        resource_version=account.version, outcome="succeeded", summary="Account profile updated",
        changes={key: {"from": before[key], "to": changes[key]} for key in changes},
        dedupe_key=f"audit:patch_my_account:{record.id}",
    )
    record.resource_type, record.resource_id, record.http_status = "platform_account", account.id, 200
    record.response_snapshot = response
    await db.commit()
    return response


@router.post("/change-user-id")
async def change_my_user_id(
    body: ChangeUserIdRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    try:
        claims = verify_enterprise_assertion(body.enterprise_assertion, audience="myclaw-user-id-link")
    except EnterpriseIdentityUnavailable:
        raise HTTPException(status_code=503, detail={"code": "enterprise_identity_unavailable"})
    except InvalidEnterpriseAssertion:
        raise HTTPException(status_code=401, detail={"code": "invalid_enterprise_assertion"})
    target_user_id = claims["user_id"]
    record, replay = await _idem(
        db, current, "change_my_user_id", idempotency_key,
        {"assertion_digest": hashlib.sha256(body.enterprise_assertion.encode()).hexdigest()},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    account = await db.scalar(select(PlatformAccount).where(
        PlatformAccount.id == current.account.id,
    ).with_for_update())
    if account.version != _expected(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "version": account.version})
    existing = await db.scalar(select(PlatformAccount.id).where(
        PlatformAccount.user_id == target_user_id, PlatformAccount.id != account.id,
    ))
    if existing:
        raise HTTPException(status_code=409, detail={"code": "user_id_already_exists"})
    identity = await db.get(UserIdentity, target_user_id)
    if identity is None:
        db.add(UserIdentity(user_id=target_user_id, status="active"))
        await db.flush()
    elif identity.status != "active":
        raise HTTPException(status_code=409, detail={"code": "identity_unavailable"})
    previous = account.user_id
    account.user_id = target_user_id
    account.version += 1
    account.auth_revision += 1
    await db.flush()
    response = await _account_view(db, account)
    await PlatformAuditWriter(db).append(
        category="identity", action="account.user_id_changed", actor_type="account",
        actor_id=account.id, actor_user_id=target_user_id,
        resource_type="platform_account", resource_id=account.id,
        resource_version=account.version, outcome="succeeded", summary="Canonical userId changed",
        changes={"user_id": {"from": previous, "to": target_user_id}},
        dedupe_key=f"audit:change_my_user_id:{record.id}",
    )
    record.resource_type, record.resource_id, record.http_status = "platform_account", account.id, 200
    record.response_snapshot = response
    await db.commit()
    return response
