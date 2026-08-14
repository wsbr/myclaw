"""Logical Model and immutable ModelVersion registry operations."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.platform_secrets import secret_reference, store_secret
from app.models.common import utcnow, uuid7
from app.models.registry import ConformanceRun, ModelVersion, RegisteredModel
from app.services.embedding_service import is_embedding_model


class ModelRegistryConflict(RuntimeError):
    pass


class ModelActivationRejected(ValueError):
    pass


def conformance_suite_version(version: ModelVersion) -> str:
    return (
        settings.embedding_conformance_suite_version
        if is_embedding_model(version)
        else settings.provider_conformance_suite_version
    )


def _hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_logical_model(
    db: AsyncSession, *, key: str, name: str, actor_account_id: str,
) -> RegisteredModel:
    if await db.scalar(select(RegisteredModel.id).where(RegisteredModel.key == key)):
        raise ModelRegistryConflict("model key already exists")
    model = RegisteredModel(
        id=uuid7(), key=key, name=name, status="active",
        current_version_id=None, next_version_no=1,
        # Transitional mirrors are not selectable by new Agent publication
        # until an immutable version passes conformance and is activated.
        provider="unconfigured", model_name="unconfigured", endpoint=None,
        credential_ref=None, default_parameters={}, capabilities={}, enabled=True,
        parameter_version=1, config_version=1,
        created_by_account_id=actor_account_id,
    )
    db.add(model)
    await db.flush()
    return model


async def create_model_version(
    db: AsyncSession,
    *,
    model_id: str,
    expected_version: int,
    values: dict[str, Any],
    actor_account_id: str,
) -> ModelVersion:
    model = await db.scalar(
        select(RegisteredModel).where(RegisteredModel.id == model_id).with_for_update()
    )
    if model is None:
        raise LookupError("model not found")
    if model.version != expected_version:
        raise ModelRegistryConflict(f"model version changed; current={model.version}")
    api_key = values.get("api_key")
    if not api_key:
        raise ValueError("model api_key is required")
    secret = await store_secret(
        db, plaintext=api_key, purpose=f"model:{model.id}",
        created_by_account_id=actor_account_id,
    )
    stored_credential = secret_reference(secret)
    payload = {
        "model_id": model.id,
        "provider": values["provider"], "model_name": values["model_name"],
        "endpoint": values.get("endpoint"), "credential_ref": stored_credential,
        "default_parameters": values.get("default_parameters") or {},
        "capabilities": values.get("capabilities") or {},
        "adapter_protocol": (
            "embedding.openai_compatible.v1"
            if (values.get("capabilities") or {}).get("model_kind") == "embedding"
            else "agent_native.v2"
        ),
    }
    hash_payload = dict(payload)
    hash_payload["credential_ref"] = f"secret-fingerprint://{secret.fingerprint}"
    content_hash = _hash(hash_payload)
    existing = await db.scalar(select(ModelVersion).where(
        ModelVersion.model_id == model.id, ModelVersion.content_hash == content_hash,
    ))
    if existing is not None:
        raise ModelRegistryConflict("identical immutable ModelVersion already exists")
    version = ModelVersion(
        id=uuid7(), model_id=model.id, version_no=model.next_version_no,
        provider=payload["provider"], model_name=payload["model_name"],
        endpoint=payload["endpoint"], credential_ref=payload["credential_ref"],
        default_parameters=payload["default_parameters"], capabilities=payload["capabilities"],
        adapter_protocol=payload["adapter_protocol"], content_hash=content_hash,
        published_at=utcnow(), published_by_account_id=actor_account_id,
    )
    db.add(version)
    model.next_version_no += 1
    model.version += 1
    await db.flush()
    return version


async def latest_terminal_conformance(
    db: AsyncSession, model_version_id: str,
) -> ConformanceRun | None:
    version = await db.get(ModelVersion, model_version_id)
    if version is None:
        return None
    return await db.scalar(
        select(ConformanceRun)
        .where(
            ConformanceRun.subject_type == "model_version",
            ConformanceRun.subject_id == model_version_id,
            ConformanceRun.suite_version == conformance_suite_version(version),
            ConformanceRun.status.in_(("passed", "failed")),
        )
        .order_by(ConformanceRun.ended_at.desc(), ConformanceRun.created_at.desc())
        .limit(1)
    )


async def activate_model_version(
    db: AsyncSession,
    *, model_id: str, model_version_id: str, expected_version: int,
) -> RegisteredModel:
    model = await db.scalar(
        select(RegisteredModel).where(RegisteredModel.id == model_id).with_for_update()
    )
    if model is None:
        raise LookupError("model not found")
    if model.version != expected_version:
        raise ModelRegistryConflict(f"model version changed; current={model.version}")
    version = await db.get(ModelVersion, model_version_id)
    if version is None or version.model_id != model.id:
        raise LookupError("model version not found")
    evidence = await latest_terminal_conformance(db, version.id)
    if evidence is None or evidence.status != "passed":
        raise ModelActivationRejected(
            f"latest {conformance_suite_version(version)} conformance did not pass"
        )
    model.current_version_id = version.id
    model.status = "active"
    # Compatibility projection for old read-only clients. Runtime and Agent
    # publication resolve only current_version_id.
    model.provider = version.provider
    model.model_name = version.model_name
    model.endpoint = version.endpoint
    model.credential_ref = version.credential_ref
    model.default_parameters = dict(version.default_parameters or {})
    model.capabilities = dict(version.capabilities or {})
    model.parameter_version += 1
    model.config_version += 1
    model.version += 1
    await db.flush()
    return model
