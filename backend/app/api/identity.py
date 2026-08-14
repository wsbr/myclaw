import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_agent_api_key
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    AgentApiKey, IdempotencyRecord,
)
from app.persistence.reliable_writes import PlatformAuditWriter
from app.services.raw_identity_service import ensure_raw_user_account

router = APIRouter(prefix="/identity", tags=["identity"])


@router.post("/preprovision")
async def preprovision_identity(
    http_response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user_id: str = Header(alias="X-MyClaw-User-Id"),
    db: AsyncSession = Depends(get_db), key: AgentApiKey = Depends(get_agent_api_key),
):
    user_id = user_id.strip()
    from app.deps import RAW_USER_ID
    if not RAW_USER_ID.fullmatch(user_id):
        raise HTTPException(status_code=400, detail={"code": "invalid_user_id"})
    request_hash = hashlib.sha256(json.dumps({"user_id": user_id}, sort_keys=True).encode()).hexdigest()
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
            http_response.status_code = record.http_status or 200
            return record.response_snapshot
    else:
        record = IdempotencyRecord(
            id=uuid7(), principal_type="agent_api_key", principal_id=key.id,
            operation="identity_preprovision", idempotency_key=idempotency_key,
            request_hash=request_hash, expires_at=utcnow() + timedelta(days=7),
        )
        db.add(record)
        await db.flush()
    try:
        account, created = await ensure_raw_user_account(
            db, user_id, tenant_id=key.tenant_id,
        )
    except ValueError:
        raise HTTPException(status_code=409, detail={"code": "identity_unavailable"})
    if created:
        await PlatformAuditWriter(db).append(
            category="identity", action="identity.preprovisioned", actor_type="agent_api_key",
            actor_id=key.id, actor_user_id=None, resource_type="platform_account",
            resource_id=account.id, resource_version=account.version, outcome="succeeded",
            summary="Preprovisioned API user identity", changes={"user_id": user_id},
            dedupe_key=f"audit:identity_preprovision:{record.id}",
        )
    body = {
        "tenant_id": account.tenant_id, "account_id": account.id,
        "user_id": user_id, "created": created,
    }
    status_code = 201 if created else 200
    record.resource_type, record.resource_id, record.http_status = (
        "platform_account", account.id, status_code,
    )
    record.response_snapshot = body
    await db.commit()
    http_response.status_code = status_code
    return body
