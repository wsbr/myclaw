"""Authoritative v2 Model registry HTTP surface."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.registries_v1 import idem, require_admin, revision
from app.config import settings
from app.core.platform_secrets import PlatformSecretError, credential_source
from app.database import get_db
from app.deps import CurrentAccount, get_current_account
from app.models.registry import ConformanceRun, ModelVersion, RegisteredModel
from app.schemas.registry_v1 import LogicalModelCreate, ModelVersionCreate
from app.services.model_registry_service import (
    ModelActivationRejected,
    ModelRegistryConflict,
    activate_model_version,
    conformance_suite_version,
    create_logical_model,
    create_model_version,
    latest_terminal_conformance,
)
from app.services.provider_conformance import execute_conformance_run
from app.services.cursor_service import SortKey, paginate_scalars


router = APIRouter(prefix="/admin/models", tags=["registries"])


def _model_view(model: RegisteredModel) -> dict:
    return {
        "id": model.id, "key": model.key, "name": model.name,
        "current_version_id": model.current_version_id,
        "status": model.status, "version": model.version,
    }


def _conformance_view(run: ConformanceRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": run.id, "subject_type": run.subject_type, "subject_id": run.subject_id,
        "suite_version": run.suite_version, "status": run.status,
        "result_summary": run.result_summary,
    }


async def _version_view(db: AsyncSession, version: ModelVersion) -> dict:
    return {
        "id": version.id, "model_id": version.model_id,
        "version_no": version.version_no, "provider": version.provider,
        "model_name": version.model_name, "endpoint": version.endpoint,
        "credential_configured": bool(version.credential_ref),
        "credential_source": credential_source(version.credential_ref),
        "default_parameters": version.default_parameters,
        "capabilities": version.capabilities,
        "latest_conformance": _conformance_view(
            await latest_terminal_conformance(db, version.id)
        ),
        "content_hash": version.content_hash,
        # The view is also persisted as an idempotency JSON snapshot. Keep it
        # JSON-native rather than relying on FastAPI's response encoder, which
        # runs only after the database transaction has committed.
        "published_at": version.published_at.isoformat() if version.published_at else None,
    }


@router.get("")
async def list_models(
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    rows, next_cursor = await paginate_scalars(
        db, select(RegisteredModel),
        sort=(SortKey(RegisteredModel.key, False), SortKey(RegisteredModel.id, False)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource="agent-native-v2-models", filters={},
    )
    return {
        "items": [_model_view(row) for row in rows],
        "next_cursor": next_cursor,
    }


@router.post("", status_code=201)
async def create_model(
    body: LogicalModelCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    record, replay = await idem(
        db, current, "create_logical_model_v2", idempotency_key,
        body.model_dump(mode="json"),
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    try:
        model = await create_logical_model(
            db, key=body.key, name=body.name, actor_account_id=current.account.id,
        )
    except ModelRegistryConflict as error:
        raise HTTPException(status_code=409, detail={"code": "model_key_exists"}) from error
    response = _model_view(model)
    record.resource_type, record.resource_id, record.http_status = "model", model.id, 201
    record.response_snapshot = response
    await db.commit()
    return response


@router.get("/{model_id}")
async def get_model(
    model_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    model = await db.get(RegisteredModel, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return _model_view(model)


@router.get("/{model_id}/versions")
async def list_versions(
    model_id: str, limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    if await db.get(RegisteredModel, model_id) is None:
        raise HTTPException(status_code=404, detail="Model not found")
    rows, next_cursor = await paginate_scalars(
        db, select(ModelVersion).where(ModelVersion.model_id == model_id),
        sort=(SortKey(ModelVersion.version_no), SortKey(ModelVersion.id)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource=f"agent-native-v2-model:{model_id}:versions", filters={},
    )
    return {
        "items": [await _version_view(db, row) for row in rows],
        "next_cursor": next_cursor,
    }


@router.post("/{model_id}/versions", status_code=201)
async def add_version(
    model_id: str, body: ModelVersionCreate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    values = body.model_dump(mode="json")
    request_snapshot = dict(values)
    request_snapshot["api_key"] = hashlib.sha256(body.api_key.encode()).hexdigest()
    record, replay = await idem(
        db, current, "create_model_version_v2", idempotency_key,
        {"model_id": model_id, **request_snapshot},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    try:
        version = await create_model_version(
            db, model_id=model_id, expected_version=revision(if_match),
            values=values, actor_account_id=current.account.id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail="Model not found") from error
    except ModelRegistryConflict as error:
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "message": str(error)}) from error
    except (PlatformSecretError, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_model_credential", "message": str(error)},
        ) from error
    response = await _version_view(db, version)
    record.resource_type, record.resource_id, record.http_status = "model_version", version.id, 201
    record.response_snapshot = response
    await db.commit()
    return response


@router.post("/{model_id}/versions/{model_version_id}/conformance", status_code=202)
async def run_conformance(
    model_id: str, model_version_id: str,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    version = await db.get(ModelVersion, model_version_id)
    if version is None or version.model_id != model_id:
        raise HTTPException(status_code=404, detail="Model version not found")
    existing = await db.scalar(select(ConformanceRun).where(
        ConformanceRun.subject_type == "model_version",
        ConformanceRun.subject_id == model_version_id,
        ConformanceRun.suite_version == conformance_suite_version(version),
        ConformanceRun.idempotency_key == idempotency_key,
    ))
    run = existing or await execute_conformance_run(
        db, version=version, idempotency_key=idempotency_key,
    )
    await db.commit()
    return _conformance_view(run)


@router.post("/{model_id}/versions/{model_version_id}/activate")
async def activate_version(
    model_id: str, model_version_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    record, replay = await idem(
        db, current, "activate_model_version_v2", idempotency_key,
        {"model_id": model_id, "model_version_id": model_version_id},
    )
    if replay and record.response_snapshot:
        return record.response_snapshot
    try:
        model = await activate_model_version(
            db, model_id=model_id, model_version_id=model_version_id,
            expected_version=revision(if_match),
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail="Model version not found") from error
    except ModelRegistryConflict as error:
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "message": str(error)}) from error
    except ModelActivationRejected as error:
        raise HTTPException(status_code=409, detail={"code": "conformance_not_passed", "message": str(error)}) from error
    response = _model_view(model)
    record.resource_type, record.resource_id, record.http_status = "model", model.id, 200
    record.response_snapshot = response
    await db.commit()
    return response
