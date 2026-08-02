from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import CurrentAccount, get_current_builder
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord
from app.models.registry import RegisteredSkill, SkillVersion
from app.persistence.reliable_writes import OutboxWriter, PlatformAuditWriter
from app.services.skill_package_service import (
    InvalidSkillPackage, receive_and_validate, store_package, sync_package,
)
from app.services.cursor_service import SortKey, paginate_scalars

router = APIRouter(prefix="/skills", tags=["skills"])


async def idem(db: AsyncSession, current: CurrentAccount, operation: str, key: str, request: dict):
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    record = await db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.principal_type == "account", IdempotencyRecord.principal_id == current.account.id,
        IdempotencyRecord.operation == operation, IdempotencyRecord.idempotency_key == key,
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


def version_view(version: SkillVersion | None) -> dict | None:
    if version is None:
        return None
    return {
        "id": version.id, "version_no": version.version_no, "status": version.status,
        "checksum": version.checksum, "manifest_name": version.manifest_name,
        "manifest_description": version.manifest_description,
        "validation_error": version.validation_error,
        "package_uri": version.package_uri, "published_at": version.published_at,
        "created_at": version.created_at,
    }


async def skill_view(db: AsyncSession, skill: RegisteredSkill) -> dict:
    current = await db.get(SkillVersion, skill.current_published_version_id) if skill.current_published_version_id else None
    return {
        "id": skill.id, "logical_name": skill.logical_name, "name": skill.name,
        "description": skill.description, "owner_account_id": skill.owner_account_id,
        "status": skill.status, "version": skill.version,
        "current_published_version_id": skill.current_published_version_id,
        "current_version": version_view(current),
    }


async def owned(db: AsyncSession, skill_id: str, current: CurrentAccount, *, lock: bool = False) -> RegisteredSkill:
    query = select(RegisteredSkill).where(RegisteredSkill.id == skill_id)
    if lock:
        query = query.with_for_update()
    skill = await db.scalar(query)
    if skill is None or (skill.owner_account_id != current.account.id and "admin" not in current.roles):
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.get("")
async def list_skills(
    owner: str | None = None, status: str | None = None,
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    query = select(RegisteredSkill)
    if "admin" not in current.roles:
        query = query.where(RegisteredSkill.owner_account_id == current.account.id)
    if owner:
        owner_id = current.account.id if owner == "me" else owner
        if owner_id != current.account.id and "admin" not in current.roles:
            raise HTTPException(status_code=403, detail="Owner filter not allowed")
        query = query.where(RegisteredSkill.owner_account_id == owner_id)
    if status:
        query = query.where(RegisteredSkill.status == status)
    rows, next_cursor = await paginate_scalars(
        db, query,
        sort=(SortKey(RegisteredSkill.logical_name, False), SortKey(RegisteredSkill.id, False)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource="skills", filters={"owner": owner, "status": status},
    )
    return {"items": [await skill_view(db, row) for row in rows], "next_cursor": next_cursor}


@router.get("/{skill_id}")
async def get_skill(
    skill_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    return await skill_view(db, await owned(db, skill_id, current))


@router.post("", status_code=201)
async def create_skill(
    logical_name: str = Form(), package: UploadFile = File(),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    try:
        temp, checksum, name, description, suffix = await receive_and_validate(package)
    except InvalidSkillPackage as error:
        raise HTTPException(status_code=422, detail={"code": "invalid_skill_package", "message": str(error)})
    record, replay = await idem(db, current, "create_skill", idempotency_key, {"logical_name": logical_name, "checksum": checksum})
    if replay and record.response_snapshot:
        temp.unlink(missing_ok=True)
        return record.response_snapshot
    if await db.scalar(select(RegisteredSkill.id).where(RegisteredSkill.logical_name == logical_name)):
        temp.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail={"code": "skill_logical_name_exists"})
    skill_id, version_id = uuid7(), uuid7()
    package_uri = store_package(temp, skill_id, version_id, suffix)
    skill = RegisteredSkill(
        id=skill_id, logical_name=logical_name, name=name, description=description,
        owner_account_id=current.account.id, status="active", version=1,
    )
    version = SkillVersion(
        id=version_id, skill_id=skill.id, version_no=1, status="uploaded",
        package_uri=package_uri, checksum=checksum, manifest_name=name,
        manifest_description=description, created_by_account_id=current.account.id,
    )
    db.add_all([skill, version])
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="registry", action="skill.created", actor_type="account",
        actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="skill", resource_id=skill.id, resource_version=skill.version,
        outcome="succeeded", summary=f"Created Skill {logical_name}",
        changes={"checksum": checksum}, dedupe_key=f"audit:create_skill:{record.id}",
    )
    response = await skill_view(db, skill)
    response["uploaded_version"] = version_view(version)
    record.resource_type, record.resource_id, record.http_status = "skill", skill.id, 201
    record.response_snapshot = json.loads(json.dumps(response, default=str))
    await db.commit()
    return response


@router.post("/{skill_id}/versions", status_code=202)
async def upload_skill_version(
    skill_id: str, package: UploadFile = File(), idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    try:
        temp, checksum, name, description, suffix = await receive_and_validate(package)
    except InvalidSkillPackage as error:
        raise HTTPException(status_code=422, detail={"code": "invalid_skill_package", "message": str(error)})
    record, replay = await idem(db, current, "upload_skill_version", idempotency_key, {"skill_id": skill_id, "checksum": checksum})
    if replay and record.response_snapshot:
        temp.unlink(missing_ok=True)
        return record.response_snapshot
    skill = await owned(db, skill_id, current, lock=True)
    if await db.scalar(select(SkillVersion.id).where(SkillVersion.skill_id == skill.id, SkillVersion.checksum == checksum)):
        temp.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail={"code": "skill_version_checksum_exists"})
    next_no = (await db.scalar(select(SkillVersion.version_no).where(
        SkillVersion.skill_id == skill.id
    ).order_by(SkillVersion.version_no.desc()).limit(1)) or 0) + 1
    version_id = uuid7()
    version = SkillVersion(
        id=version_id, skill_id=skill.id, version_no=next_no, status="uploaded",
        package_uri=store_package(temp, skill.id, version_id, suffix), checksum=checksum,
        manifest_name=name, manifest_description=description,
        created_by_account_id=current.account.id,
    )
    db.add(version)
    await db.flush()
    response = version_view(version)
    record.resource_type, record.resource_id, record.http_status = "skill_version", version.id, 202
    record.response_snapshot = json.loads(json.dumps(response, default=str))
    await db.commit()
    return response


@router.get("/{skill_id}/versions")
async def list_skill_versions(
    skill_id: str, status: str | None = None, limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_builder),
):
    skill = await owned(db, skill_id, current)
    query = select(SkillVersion).where(SkillVersion.skill_id == skill.id)
    if status:
        query = query.where(SkillVersion.status == status)
    rows, next_cursor = await paginate_scalars(
        db, query, sort=(SortKey(SkillVersion.version_no), SortKey(SkillVersion.id)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource=f"skill:{skill_id}:versions", filters={"status": status},
    )
    return {"items": [version_view(row) for row in rows], "next_cursor": next_cursor}


@router.post("/{skill_id}/versions/{version_id}/publish")
async def publish_skill_version(
    skill_id: str, version_id: str, idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    record, replay = await idem(db, current, "publish_skill_version", idempotency_key, {"skill_id": skill_id, "version_id": version_id})
    if replay and record.response_snapshot:
        return record.response_snapshot
    skill = await owned(db, skill_id, current, lock=True)
    version = await db.scalar(select(SkillVersion).where(
        SkillVersion.id == version_id, SkillVersion.skill_id == skill.id,
    ).with_for_update())
    if version is None:
        raise HTTPException(status_code=404, detail="Skill version not found")
    if version.status not in {"uploaded", "validating", "published"}:
        raise HTTPException(status_code=409, detail={"code": "skill_version_not_publishable"})
    if skill.current_published_version_id and skill.current_published_version_id != version.id:
        previous = await db.get(SkillVersion, skill.current_published_version_id)
        if previous and previous.status == "published":
            previous.status = "deprecated"
    version.status = "published"
    version.published_at = utcnow()
    skill.current_published_version_id = version.id
    skill.name, skill.description = version.manifest_name, version.manifest_description
    skill.version += 1
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="registry", action="skill.version_published", actor_type="account",
        actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="skill_version", resource_id=version.id, resource_version=version.version_no,
        outcome="succeeded", summary=f"Published Skill {skill.logical_name} v{version.version_no}",
        changes={"checksum": version.checksum}, dedupe_key=f"audit:publish_skill:{record.id}",
    )
    await OutboxWriter(db).enqueue(
        topic="runtime.maintenance", action="sync_skill", aggregate_type="skill_version",
        aggregate_id=version.id, partition_key=skill.id, dedupe_key=f"skill_version:{version.id}:sync",
        payload_ref={"skill_id": skill.id, "version_id": version.id},
    )
    response = {**version_view(version), "sync_status": "queued"}
    record.resource_type, record.resource_id, record.http_status = "skill_version", version.id, 200
    record.response_snapshot = json.loads(json.dumps(response, default=str))
    await db.commit()
    return response


@router.post("/{skill_id}/sync")
async def sync_skill(
    skill_id: str, idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder),
):
    record, replay = await idem(db, current, "sync_skill", idempotency_key, {"skill_id": skill_id})
    if replay and record.response_snapshot:
        return record.response_snapshot
    skill = await owned(db, skill_id, current)
    version = await db.get(SkillVersion, skill.current_published_version_id) if skill.current_published_version_id else None
    if version is None or version.status != "published":
        raise HTTPException(status_code=409, detail={"code": "skill_has_no_published_version"})
    try:
        path_uri = sync_package(skill.id, version.checksum, version.package_uri)
    except InvalidSkillPackage as error:
        raise HTTPException(status_code=409, detail={"code": "skill_sync_failed", "message": str(error)})
    response = {**version_view(version), "sync_status": "succeeded", "path_uri": path_uri}
    record.resource_type, record.resource_id, record.http_status = "skill", skill.id, 200
    record.response_snapshot = json.loads(json.dumps(response, default=str))
    await db.commit()
    return response
