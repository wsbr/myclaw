"""Secure lifecycle operations for Agent-bound API credentials."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_v1 import AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import AgentApiKey
from app.models.tenant_v1 import Tenant
from app.persistence.reliable_writes import PlatformAuditWriter


def _new_secret() -> tuple[str, str, str]:
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    plaintext = f"myclaw_{prefix}_{secret}"
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    return prefix, plaintext, digest


def verify_api_key(plaintext: str, stored_hash: str) -> bool:
    return secrets.compare_digest(hashlib.sha256(plaintext.encode()).hexdigest(), stored_hash)


async def create_api_key(
    db: AsyncSession,
    *,
    agent_id: str,
    tenant_id: str,
    name: str,
    caller_label: str,
    created_by_account_id: str,
    expires_at: datetime | None,
    idempotency_record_id: str,
    rotated_from_id: str | None = None,
) -> tuple[AgentApiKey, str]:
    prefix, plaintext, digest = _new_secret()
    key = AgentApiKey(
        id=uuid7(), tenant_id=tenant_id, agent_id=agent_id,
        name=name, caller_label=caller_label,
        prefix=prefix, secret_hash=digest, status="active",
        created_by_account_id=created_by_account_id, expires_at=expires_at,
        rotated_from_id=rotated_from_id,
    )
    db.add(key)
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="security", action="agent_api_key.created", actor_type="account",
        actor_id=created_by_account_id, actor_user_id=None, resource_type="agent_api_key",
        resource_id=key.id, resource_version=key.version, outcome="succeeded",
        summary=f"Created API key {key.prefix} for Agent", changes={"status": "active"},
        dedupe_key=f"audit:create_agent_api_key:{idempotency_record_id}",
    )
    return key, plaintext


async def revoke_api_key(
    db: AsyncSession,
    *,
    key_id: str,
    actor_account_id: str,
    idempotency_record_id: str,
) -> AgentApiKey:
    key = await db.scalar(select(AgentApiKey).where(AgentApiKey.id == key_id).with_for_update())
    if key is None:
        raise LookupError("API key not found")
    if key.status != "revoked":
        key.status = "revoked"
        key.version += 1
        await db.flush()
        await PlatformAuditWriter(db).append(
            category="security", action="agent_api_key.revoked", actor_type="account",
            actor_id=actor_account_id, actor_user_id=None, resource_type="agent_api_key",
            resource_id=key.id, resource_version=key.version, outcome="succeeded",
            summary=f"Revoked API key {key.prefix}", changes={"status": "revoked"},
            dedupe_key=f"audit:revoke_agent_api_key:{idempotency_record_id}",
        )
    return key


async def authenticate_api_key(db: AsyncSession, plaintext: str) -> AgentApiKey | None:
    parts = plaintext.split("_", 2)
    if len(parts) != 3 or parts[0] != "myclaw":
        return None
    key = await db.scalar(select(AgentApiKey).where(AgentApiKey.prefix == parts[1]))
    if key is None or key.status != "active" or not verify_api_key(plaintext, key.secret_hash):
        return None
    tenant = await db.get(Tenant, key.tenant_id)
    agent = await db.get(AgentBlueprint, key.agent_id)
    if (
        tenant is None or tenant.status != "active"
        or agent is None or agent.tenant_id != key.tenant_id
        or agent.status not in {"draft", "published"}
    ):
        return None
    if key.expires_at is not None and key.expires_at <= utcnow():
        key.status = "expired"
        key.version += 1
        await db.flush()
        return None
    key.last_used_at = utcnow()
    return key
