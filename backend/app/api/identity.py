import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_agent_api_key
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    AgentApiKey, IdempotencyRecord, PlatformAccount, PlatformAccountRole, UserIdentity,
)
from app.persistence.reliable_writes import PlatformAuditWriter

router = APIRouter(prefix="/identity", tags=["identity"])


class PreprovisionRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=191, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")


@router.post("/preprovision")
async def preprovision_identity(
    body: PreprovisionRequest, idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), key: AgentApiKey = Depends(get_agent_api_key),
):
    request_hash = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    record = await db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.principal_type == "agent_api_key",
        IdempotencyRecord.principal_id == key.id,
        IdempotencyRecord.operation == "identity_preprovision",
        IdempotencyRecord.idempotency_key == idempotency_key,
    ))
    if record:
        if record.request_hash != request_hash:
            raise HTTPException(status_code=409, detail={"code": "idempotency_mismatch"})
        if record.response_snapshot:
            return record.response_snapshot
    else:
        record = IdempotencyRecord(
            id=uuid7(), principal_type="agent_api_key", principal_id=key.id,
            operation="identity_preprovision", idempotency_key=idempotency_key,
            request_hash=request_hash, expires_at=utcnow() + timedelta(days=7),
        )
        db.add(record)
        await db.flush()
    identity = await db.scalar(select(UserIdentity).where(
        UserIdentity.user_id == body.user_id
    ).with_for_update())
    created = False
    if identity is None:
        identity = UserIdentity(user_id=body.user_id, status="active")
        db.add(identity)
        await db.flush()
    elif identity.status != "active":
        raise HTTPException(status_code=409, detail={"code": "identity_unavailable"})
    account = await db.scalar(select(PlatformAccount).where(
        PlatformAccount.user_id == body.user_id
    ).with_for_update())
    if account is None:
        created = True
        account = PlatformAccount(
            id=uuid7(), user_id=body.user_id, display_name=body.user_id,
            password_hash=None, status="active", auth_revision=1,
        )
        db.add(account)
        await db.flush()
        db.add(PlatformAccountRole(
            account_id=account.id, role_key="user", granted_by_account_id=None,
            granted_at=utcnow(),
        ))
        await PlatformAuditWriter(db).append(
            category="identity", action="identity.preprovisioned", actor_type="agent_api_key",
            actor_id=key.id, actor_user_id=None, resource_type="platform_account",
            resource_id=account.id, resource_version=account.version, outcome="succeeded",
            summary="Preprovisioned API user identity", changes={"user_id": body.user_id},
            dedupe_key=f"audit:identity_preprovision:{record.id}",
        )
    response = {"account_id": account.id, "user_id": body.user_id, "created": created}
    record.resource_type, record.resource_id, record.http_status = "platform_account", account.id, 200
    record.response_snapshot = response
    await db.commit()
    return response
