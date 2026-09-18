"""Publish immutable policy parameters while preserving existing Agent bindings."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import utcnow, uuid7
from app.models.registry import PolicyDefinition, PolicyVersion


async def publish_policy_version(
    db: AsyncSession, policy: PolicyDefinition,
) -> PolicyVersion:
    """Select/publish immutable content; caller holds the definition row lock."""

    parameters = dict(policy.default_parameters or {})
    content = {
        "implementation_ref": policy.implementation_ref,
        "parameters": parameters,
        "policy_type": policy.policy_type,
    }
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    version = await db.scalar(select(PolicyVersion).where(
        PolicyVersion.policy_definition_id == policy.id,
        PolicyVersion.content_hash == digest,
    ))
    if version is None:
        version = PolicyVersion(
            id=uuid7(), policy_definition_id=policy.id,
            version_no=policy.next_version_no,
            implementation_ref=policy.implementation_ref, parameters=parameters,
            content_hash=digest, status="published", published_at=utcnow(), version=1,
        )
        db.add(version)
        await db.flush()
        policy.next_version_no += 1
    elif version.status != "published":
        raise ValueError("matching immutable PolicyVersion is unavailable")
    policy.current_version_id = version.id
    return version
