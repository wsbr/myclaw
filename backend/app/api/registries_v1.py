from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.model_hub.registry import ModelHub
from app.database import get_db
from app.deps import CurrentAccount, get_current_account
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord
from app.models.registry import PlatformTool, PolicyDefinition, RegisteredModel, SandboxProfile
from app.persistence.reliable_writes import PlatformAuditWriter
from app.schemas.registry_v1 import ModelCreate, ModelPatch, PlatformToolPatch, PolicyPatch, SandboxPatch
from app.services.registry_reconcile import legacy_builtin_specs, reconcile_platform_tools
from app.services.cursor_service import SortKey, paginate_scalars

router = APIRouter(tags=["registries"])


def require_admin(current: CurrentAccount) -> None:
    if "admin" not in current.roles:
        raise HTTPException(status_code=403, detail="Admin required")


def revision(value: str | None) -> int:
    match = re.search(r"(\d+)", value or "")
    if not match:
        raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    return int(match.group(1))


async def idem(
    db: AsyncSession, current: CurrentAccount, operation: str, key: str, request: dict,
) -> tuple[IdempotencyRecord, bool]:
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
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


def model_view(model: RegisteredModel) -> dict:
    return {
        "id": model.id, "key": model.key, "provider": model.provider,
        "model_name": model.model_name, "endpoint": model.endpoint,
        "credential_configured": bool(model.credential_ref),
        "default_parameters": model.default_parameters, "parameter_version": model.parameter_version,
        "capabilities": model.capabilities, "enabled": model.enabled,
        "version": model.config_version,
        "created_at": model.created_at.isoformat() if model.created_at else None,
        "updated_at": model.updated_at.isoformat() if model.updated_at else None,
    }


@router.get("/models")
async def list_models(
    provider: str | None = None, enabled: bool | None = None,
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    query = select(RegisteredModel)
    if provider: query = query.where(RegisteredModel.provider == provider)
    if enabled is not None: query = query.where(RegisteredModel.enabled == enabled)
    rows, next_cursor = await paginate_scalars(
        db, query, sort=(SortKey(RegisteredModel.key, False), SortKey(RegisteredModel.id, False)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource="models", filters={"provider": provider, "enabled": enabled},
    )
    return {"items": [model_view(row) for row in rows], "next_cursor": next_cursor}


@router.post("/models", status_code=201)
async def create_model(
    body: ModelCreate, idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    record, replay = await idem(db, current, "create_model", idempotency_key, body.model_dump(mode="json"))
    if replay and record.response_snapshot: return record.response_snapshot
    if await db.scalar(select(RegisteredModel.id).where(RegisteredModel.key == body.key)):
        raise HTTPException(status_code=409, detail={"code": "model_key_exists"})
    model = RegisteredModel(
        id=uuid7(), key=body.key, provider=body.provider, model_name=body.model_name,
        endpoint=body.endpoint, credential_ref=body.credential_ref,
        default_parameters=body.default_parameters, capabilities=body.capabilities,
        enabled=body.enabled, parameter_version=1, config_version=1,
        created_by_account_id=current.account.id,
    )
    db.add(model)
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="registry", action="model.created", actor_type="account",
        actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="model", resource_id=model.id, resource_version=1,
        outcome="succeeded", summary=f"Created model {model.key}", changes={"enabled": model.enabled},
        dedupe_key=f"audit:create_model:{record.id}",
    )
    response = model_view(model)
    record.resource_type, record.resource_id, record.http_status = "model", model.id, 201
    record.response_snapshot = response
    await db.commit()
    return response


@router.get("/models/{model_id}")
async def get_model(
    model_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    model = await db.get(RegisteredModel, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return model_view(model)


@router.patch("/models/{model_id}")
async def patch_model(
    model_id: str, body: ModelPatch, if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"), db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    record, replay = await idem(db, current, "patch_model", idempotency_key, {"id": model_id, **body.model_dump(mode="json")})
    if replay and record.response_snapshot: return record.response_snapshot
    model = await db.scalar(select(RegisteredModel).where(RegisteredModel.id == model_id).with_for_update())
    if model is None: raise HTTPException(status_code=404, detail="Model not found")
    if model.config_version != revision(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": model.config_version})
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items(): setattr(model, field, value)
    if "default_parameters" in changes: model.parameter_version += 1
    model.config_version += 1
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="registry", action="model.updated", actor_type="account", actor_id=current.account.id,
        actor_user_id=current.account.user_id, resource_type="model", resource_id=model.id,
        resource_version=model.config_version, outcome="succeeded", summary=f"Updated model {model.key}",
        changes={"fields": sorted(changes)}, dedupe_key=f"audit:patch_model:{record.id}",
    )
    response = model_view(model)
    record.resource_type, record.resource_id, record.http_status = "model", model.id, 200
    record.response_snapshot = response
    await db.commit()
    return response


@router.post("/models/{model_id}/connection-test")
async def test_model_connection(
    model_id: str, db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    model = await db.get(RegisteredModel, model_id)
    if model is None: raise HTTPException(status_code=404, detail="Model not found")
    credential = "local-development-key"
    if model.credential_ref and model.credential_ref.startswith("env://"):
        credential = os.getenv(model.credential_ref[6:], "")
    started = time.perf_counter()
    try:
        adapter = ModelHub.get({}, model.provider, credential, model.endpoint)
        await adapter.count_tokens([{"role": "user", "content": "ping"}])
        return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000)}
    except Exception as error:
        return {"ok": False, "latency_ms": round((time.perf_counter() - started) * 1000), "provider_error_code": type(error).__name__}


@router.post("/models/{model_id}/{command}")
async def set_model_enabled(
    model_id: str, command: str, idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    if command not in {"enable", "disable"}:
        raise HTTPException(status_code=404, detail="Command not found")
    model = await db.scalar(select(RegisteredModel).where(RegisteredModel.id == model_id).with_for_update())
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")
    record, replay = await idem(db, current, f"{command}_model", idempotency_key, {"id": model_id})
    if replay and record.response_snapshot:
        return record.response_snapshot
    target = command == "enable"
    if model.enabled != target:
        model.enabled = target
        model.config_version += 1
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="registry", action=f"model.{command}d", actor_type="account",
        actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="model", resource_id=model.id, resource_version=model.config_version,
        outcome="succeeded", summary=f"{command.title()}d model {model.key}",
        changes={"enabled": target}, dedupe_key=f"audit:{command}_model:{record.id}",
    )
    response = model_view(model)
    record.resource_type, record.resource_id, record.http_status = "model", model.id, 200
    record.response_snapshot = response
    await db.commit()
    return response


def _redact_config(value):
    if not isinstance(value, dict):
        return value
    safe = {}
    for key, item in value.items():
        lowered = key.lower()
        if any(marker in lowered for marker in ("secret", "password", "token", "api_key", "credential")):
            safe[key] = {"configured": item not in (None, "")}
        elif isinstance(item, dict):
            safe[key] = _redact_config(item)
        else:
            safe[key] = item
    return safe


def tool_view(tool: PlatformTool) -> dict:
    return {"id": tool.id, "tool_key": tool.tool_key, "display_name": tool.display_name,
            "description": tool.description, "input_schema": tool.input_schema, "output_schema": tool.output_schema,
            "implementation_ref": tool.implementation_ref, "implementation_version": tool.implementation_version,
            "execution_mode": tool.execution_mode, "enabled": tool.enabled,
            "confirmation_policy": tool.confirmation_policy, "platform_config": _redact_config(tool.platform_config),
            "editable_config_schema": (tool.platform_config or {}).get("editable_config_schema", {}),
            "usage_count": 0, "version": tool.config_version,
            "config_version": tool.config_version, "registration_hash": tool.registration_hash}


@router.get("/platform-tools")
async def list_platform_tools(
    enabled: bool | None = None, execution_mode: str | None = None,
    confirmation_policy: str | None = None,
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    query = select(PlatformTool)
    if enabled is not None:
        query = query.where(PlatformTool.enabled == enabled)
    if execution_mode:
        query = query.where(PlatformTool.execution_mode == execution_mode)
    if confirmation_policy:
        query = query.where(PlatformTool.confirmation_policy == confirmation_policy)
    filters = {"enabled": enabled, "execution_mode": execution_mode,
               "confirmation_policy": confirmation_policy}
    rows, next_cursor = await paginate_scalars(
        db, query, sort=(SortKey(PlatformTool.tool_key, False), SortKey(PlatformTool.id, False)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource="platform-tools", filters=filters,
    )
    return {"items": [tool_view(row) for row in rows], "next_cursor": next_cursor}


@router.get("/platform-tools/{tool_id}")
async def get_platform_tool(
    tool_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    tool = await db.get(PlatformTool, tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    return tool_view(tool)


@router.patch("/platform-tools/{tool_id}")
async def patch_platform_tool(
    tool_id: str, body: PlatformToolPatch, if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    record, replay = await idem(db, current, "patch_platform_tool", idempotency_key, {"id": tool_id, **body.model_dump(mode="json")})
    if replay and record.response_snapshot:
        return record.response_snapshot
    tool = await db.scalar(select(PlatformTool).where(PlatformTool.id == tool_id).with_for_update())
    if tool is None: raise HTTPException(status_code=404, detail="Tool not found")
    if tool.config_version != revision(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": tool.config_version})
    changes = body.model_dump(exclude_unset=True)
    if changes.get("confirmation_policy") not in (
        None, "never", "always", "per_call", "session_grant",
    ):
        raise HTTPException(status_code=422, detail={"code": "invalid_confirmation_policy"})
    workspace_access = (changes.get("platform_config") or {}).get("workspace_access")
    if workspace_access not in (None, "none", "read", "write"):
        raise HTTPException(status_code=422, detail={"code": "invalid_workspace_access"})
    for field, value in changes.items(): setattr(tool, field, value)
    tool.config_version += 1
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="registry", action="platform_tool.updated", actor_type="account",
        actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="platform_tool", resource_id=tool.id, resource_version=tool.config_version,
        outcome="succeeded", summary=f"Updated platform tool {tool.tool_key}",
        changes={"fields": sorted(changes)}, dedupe_key=f"audit:patch_platform_tool:{record.id}",
    )
    response = tool_view(tool)
    record.resource_type, record.resource_id, record.http_status = "platform_tool", tool.id, 200
    record.response_snapshot = response
    await db.commit()
    return response


@router.post("/platform-tools/reconcile")
async def reconcile_tools(
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    record, replay = await idem(db, current, "reconcile_platform_tools", idempotency_key, {})
    if replay and record.response_snapshot:
        return record.response_snapshot
    before = {row.tool_key: row.registration_hash for row in await db.scalars(select(PlatformTool))}
    tools = await reconcile_platform_tools(db, legacy_builtin_specs(), job_run_id=record.id)
    created = sum(1 for tool in tools if tool.tool_key not in before)
    updated = sum(1 for tool in tools if tool.tool_key in before and before[tool.tool_key] != tool.registration_hash)
    response = {"created": created, "updated": updated, "registration_missing": [], "audit_event_id": None}
    record.resource_type, record.resource_id, record.http_status = "platform_tool_registry", "builtin", 200
    record.response_snapshot = response
    await db.commit()
    return response


@router.post("/platform-tools/{tool_id}/connection-test")
async def test_platform_tool_connection(
    tool_id: str, db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    tool = await db.get(PlatformTool, tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    return {"ok": tool.enabled, "latency_ms": 0, "provider_error_code": None if tool.enabled else "tool_disabled"}


def policy_view(policy: PolicyDefinition) -> dict:
    return {"id": policy.id, "policy_key": policy.policy_key, "policy_type": policy.policy_type,
            "name": policy.name, "description": policy.description,
            "default_parameters": policy.default_parameters, "parameter_version": policy.parameter_version,
            "enabled": policy.enabled}


def sandbox_view(profile: SandboxProfile) -> dict:
    return {"id": profile.id, "profile_key": profile.profile_key, "type": profile.type,
            "description": profile.description, "config": profile.config,
            "config_version": profile.config_version, "enabled": profile.enabled}


@router.get("/policy-definitions")
async def list_policies(
    policy_type: str | None = None, enabled: bool | None = None,
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    query = select(PolicyDefinition)
    if policy_type:
        query = query.where(PolicyDefinition.policy_type == policy_type)
    if enabled is not None:
        query = query.where(PolicyDefinition.enabled == enabled)
    rows, next_cursor = await paginate_scalars(
        db, query, sort=(SortKey(PolicyDefinition.policy_key, False), SortKey(PolicyDefinition.id, False)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource="policy-definitions", filters={"policy_type": policy_type, "enabled": enabled},
    )
    return {"items": [policy_view(row) for row in rows], "next_cursor": next_cursor}


@router.get("/policy-definitions/{policy_id}")
async def get_policy(policy_id: str, db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account)):
    require_admin(current)
    policy = await db.get(PolicyDefinition, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    return policy_view(policy)


@router.patch("/policy-definitions/{policy_id}")
async def patch_policy(
    policy_id: str, body: PolicyPatch, if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    record, replay = await idem(db, current, "patch_policy_definition", idempotency_key, {"id": policy_id, **body.model_dump(mode="json")})
    if replay and record.response_snapshot:
        return record.response_snapshot
    policy = await db.scalar(select(PolicyDefinition).where(PolicyDefinition.id == policy_id).with_for_update())
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    if policy.parameter_version != revision(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": policy.parameter_version})
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(policy, field, value)
    policy.parameter_version += 1
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="registry", action="policy_definition.updated", actor_type="account",
        actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="policy_definition", resource_id=policy.id, resource_version=policy.parameter_version,
        outcome="succeeded", summary=f"Updated policy {policy.policy_key}",
        changes={"fields": sorted(changes)}, dedupe_key=f"audit:patch_policy:{record.id}",
    )
    response = policy_view(policy)
    record.resource_type, record.resource_id, record.http_status = "policy_definition", policy.id, 200
    record.response_snapshot = response
    await db.commit()
    return response


@router.get("/sandbox-profiles")
async def list_sandboxes(
    enabled: bool | None = None, limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    query = select(SandboxProfile)
    if enabled is not None:
        query = query.where(SandboxProfile.enabled == enabled)
    rows, next_cursor = await paginate_scalars(
        db, query, sort=(SortKey(SandboxProfile.profile_key, False), SortKey(SandboxProfile.id, False)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource="sandbox-profiles", filters={"enabled": enabled},
    )
    return {"items": [sandbox_view(row) for row in rows], "next_cursor": next_cursor}


@router.get("/sandbox-profiles/{profile_id}")
async def get_sandbox(profile_id: str, db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account)):
    require_admin(current)
    profile = await db.get(SandboxProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Sandbox profile not found")
    return sandbox_view(profile)


@router.patch("/sandbox-profiles/{profile_id}")
async def patch_sandbox(
    profile_id: str, body: SandboxPatch, if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    record, replay = await idem(db, current, "patch_sandbox_profile", idempotency_key, {"id": profile_id, **body.model_dump(mode="json")})
    if replay and record.response_snapshot:
        return record.response_snapshot
    profile = await db.scalar(select(SandboxProfile).where(SandboxProfile.id == profile_id).with_for_update())
    if profile is None:
        raise HTTPException(status_code=404, detail="Sandbox profile not found")
    if profile.config_version != revision(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": profile.config_version})
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(profile, field, value)
    profile.config_version += 1
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="registry", action="sandbox_profile.updated", actor_type="account",
        actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="sandbox_profile", resource_id=profile.id, resource_version=profile.config_version,
        outcome="succeeded", summary=f"Updated sandbox {profile.profile_key}",
        changes={"fields": sorted(changes)}, dedupe_key=f"audit:patch_sandbox:{record.id}",
    )
    response = sandbox_view(profile)
    record.resource_type, record.resource_id, record.http_status = "sandbox_profile", profile.id, 200
    record.response_snapshot = response
    await db.commit()
    return response
