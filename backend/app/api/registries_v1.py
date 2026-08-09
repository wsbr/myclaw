from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.model_hub.registry import ModelHub
from app.core.platform_secrets import PlatformSecretError, secret_reference, store_secret
from app.database import get_db
from app.deps import CurrentAccount, get_current_account
from app.models.common import utcnow, uuid7
from app.models.foundation import IdempotencyRecord
from app.models.registry import (
    PolicyDefinition, RegisteredModel, SandboxProfile, ToolConnection,
    ToolConnectionRevision, ToolDefinition, ToolGovernance, ToolVersion,
)
from app.persistence.reliable_writes import PlatformAuditWriter
from app.schemas.registry_v1 import (
    ModelCreate, ModelPatch, PlatformToolPatch, PolicyPatch, SandboxPatch,
    ToolConnectionPatch, ToolConnectionUpsert,
)
from app.services.registry_reconcile import builtin_tool_specs, reconcile_platform_tools
from app.services.provider_conformance import resolve_provider_credential
from app.services.cursor_service import SortKey, paginate_scalars
from app.services.mcp_tool_service import McpToolError, mcp_initialize_and_list, sync_mcp_tools
from app.services.tool_connection_service import (
    ToolConnectionError, _assert_public_config, connection_view, create_or_update_connection,
    resolve_connection_revision,
)
from app.services.web_tool_service import WebToolError, web_search

router = APIRouter(tags=["registries"])


async def _store_model_credential(db, *, api_key, model_key, actor_id):
    if not api_key:
        raise PlatformSecretError("api_key is required")
    secret = await store_secret(
        db, plaintext=api_key, purpose=f"model:{model_key}",
        created_by_account_id=actor_id,
    )
    return secret_reference(secret)


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
    request_snapshot = body.model_dump(mode="json")
    if body.api_key:
        request_snapshot["api_key"] = hashlib.sha256(body.api_key.encode()).hexdigest()
    record, replay = await idem(db, current, "create_model", idempotency_key, request_snapshot)
    if replay and record.response_snapshot: return record.response_snapshot
    if await db.scalar(select(RegisteredModel.id).where(RegisteredModel.key == body.key)):
        raise HTTPException(status_code=409, detail={"code": "model_key_exists"})
    try:
        stored_credential = await _store_model_credential(
            db, api_key=body.api_key,
            model_key=body.key, actor_id=current.account.id,
        )
    except PlatformSecretError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_model_credential", "message": str(error)},
        ) from error
    model = RegisteredModel(
        id=uuid7(), key=body.key, provider=body.provider, model_name=body.model_name,
        endpoint=body.endpoint, credential_ref=stored_credential,
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
    request_snapshot = body.model_dump(mode="json", exclude_unset=True)
    if body.api_key:
        request_snapshot["api_key"] = hashlib.sha256(body.api_key.encode()).hexdigest()
    record, replay = await idem(
        db, current, "patch_model", idempotency_key,
        {"id": model_id, **request_snapshot},
    )
    if replay and record.response_snapshot: return record.response_snapshot
    model = await db.scalar(select(RegisteredModel).where(RegisteredModel.id == model_id).with_for_update())
    if model is None: raise HTTPException(status_code=404, detail="Model not found")
    if model.config_version != revision(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": model.config_version})
    changes = body.model_dump(exclude_unset=True)
    if "api_key" in changes:
        try:
            changes["credential_ref"] = await _store_model_credential(
                db, api_key=changes.pop("api_key", None),
                model_key=model.key, actor_id=current.account.id,
            )
        except PlatformSecretError as error:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_model_credential", "message": str(error)},
            ) from error
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
    credential = await resolve_provider_credential(db, model.credential_ref)
    started = time.perf_counter()
    adapter = None
    try:
        adapter = ModelHub.get({}, model.provider, credential, model.endpoint)
        # Token counting is local for OpenAI-compatible adapters, so it cannot
        # prove that the endpoint, model name, and credential actually work.
        await asyncio.wait_for(
            adapter.chat(
                [{"role": "user", "content": "Reply with OK."}],
                None,
                {"model_id": model.model_name, "max_tokens": 1, "temperature": 0},
            ),
            timeout=30,
        )
        return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000)}
    except Exception as error:
        return {"ok": False, "latency_ms": round((time.perf_counter() - started) * 1000), "provider_error_code": type(error).__name__}
    finally:
        client = getattr(adapter, "client", None)
        close = getattr(client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass


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
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
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


def tool_view(
    tool: ToolDefinition, version_row: ToolVersion | None,
    governance: ToolGovernance | None,
) -> dict:
    return {
        "id": tool.id, "tool_key": tool.tool_key, "namespace": tool.namespace,
        "origin": tool.origin, "display_name": tool.display_name,
        "display_name_zh": tool.display_name_zh, "description": tool.summary,
        "status": tool.status, "current_version_id": tool.current_version_id,
        "tool_version_id": version_row.id if version_row else None,
        "version_no": version_row.version_no if version_row else None,
        "input_schema": version_row.input_schema if version_row else {},
        "output_schema": version_row.output_schema if version_row else {},
        "examples": version_row.examples if version_row else [],
        "error_catalog": version_row.error_catalog if version_row else [],
        "annotations": version_row.annotations if version_row else {},
        "implementation_ref": version_row.implementation_ref if version_row else None,
        "implementation_version": version_row.implementation_version if version_row else None,
        "execution_mode": version_row.execution_mode if version_row else None,
        "side_effect": version_row.side_effect if version_row else None,
        "content_hash": version_row.content_hash if version_row else None,
        "enabled": bool(governance and governance.enabled and tool.status == "active"),
        "confirmation_policy": governance.confirmation_policy if governance else "never",
        "network_policy": _redact_config(governance.network_policy if governance else {}),
        "workspace_policy": _redact_config(governance.workspace_policy if governance else {}),
        "risk_rules": governance.risk_rules if governance else [],
        "usage_count": 0,
        "version": governance.version if governance else tool.version,
        "config_version": governance.version if governance else tool.version,
    }


async def _tool_view(db: AsyncSession, tool: ToolDefinition) -> dict:
    version_row = await db.get(ToolVersion, tool.current_version_id) if tool.current_version_id else None
    governance = await db.get(ToolGovernance, tool.id)
    return tool_view(tool, version_row, governance)


@router.get("/platform-tools")
async def list_platform_tools(
    enabled: bool | None = None, execution_mode: str | None = None,
    confirmation_policy: str | None = None,
    limit: int = 50, cursor: str | None = None,
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    query = select(ToolDefinition)
    filters = {"enabled": enabled, "execution_mode": execution_mode,
               "confirmation_policy": confirmation_policy}
    rows, next_cursor = await paginate_scalars(
        db, query, sort=(SortKey(ToolDefinition.tool_key, False), SortKey(ToolDefinition.id, False)),
        limit=limit, cursor=cursor, principal=f"account:{current.account.id}",
        resource="platform-tools", filters=filters,
    )
    views = [await _tool_view(db, row) for row in rows]
    if enabled is not None:
        views = [item for item in views if item["enabled"] is enabled]
    if execution_mode:
        views = [item for item in views if item["execution_mode"] == execution_mode]
    if confirmation_policy:
        views = [item for item in views if item["confirmation_policy"] == confirmation_policy]
    return {"items": views, "next_cursor": next_cursor}


@router.get("/platform-tools/{tool_id}")
async def get_platform_tool(
    tool_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    tool = await db.get(ToolDefinition, tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    return await _tool_view(db, tool)


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
    tool = await db.scalar(select(ToolDefinition).where(ToolDefinition.id == tool_id).with_for_update())
    if tool is None: raise HTTPException(status_code=404, detail="Tool not found")
    governance = await db.scalar(select(ToolGovernance).where(
        ToolGovernance.tool_definition_id == tool.id,
    ).with_for_update())
    if governance is None:
        raise HTTPException(status_code=409, detail={"code": "tool_governance_missing"})
    if governance.version != revision(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": governance.version})
    changes = body.model_dump(exclude_unset=True)
    try:
        for policy_field in ("network_policy", "workspace_policy", "risk_rules"):
            if policy_field in changes:
                _assert_public_config(changes[policy_field], path=policy_field)
    except ToolConnectionError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "credential_in_governance", "message": str(error)},
        ) from error
    if changes.get("confirmation_policy") not in (
        None, "never", "per_call", "session_grant",
    ):
        raise HTTPException(status_code=422, detail={"code": "invalid_confirmation_policy"})
    workspace_access = (changes.get("workspace_policy") or {}).get("workspace_access")
    if workspace_access not in (None, "none", "read", "write"):
        raise HTTPException(status_code=422, detail={"code": "invalid_workspace_access"})
    if "display_name" in changes:
        tool.display_name = changes.pop("display_name")
        tool.version += 1
    if "display_name_zh" in changes:
        tool.display_name_zh = changes.pop("display_name_zh")
        tool.version += 1
    for field, value in changes.items(): setattr(governance, field, value)
    governance.version += 1
    await db.flush()
    await PlatformAuditWriter(db).append(
        category="registry", action="platform_tool.updated", actor_type="account",
        actor_id=current.account.id, actor_user_id=current.account.user_id,
        resource_type="tool_governance", resource_id=tool.id, resource_version=governance.version,
        outcome="succeeded", summary=f"Updated platform tool {tool.tool_key}",
        changes={"fields": sorted(changes)}, dedupe_key=f"audit:patch_platform_tool:{record.id}",
    )
    response = await _tool_view(db, tool)
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
    existing = list(await db.scalars(select(ToolDefinition)))
    before = {row.tool_key: row.current_version_id for row in existing}
    tools = await reconcile_platform_tools(db, builtin_tool_specs(), job_run_id=record.id)
    created = sum(1 for tool in tools if tool.tool_key not in before)
    updated = sum(1 for tool in tools if tool.tool_key in before and before[tool.tool_key] != tool.current_version_id)
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
    tool = await db.get(ToolDefinition, tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    if tool.tool_key == "web_search":
        try:
            connection, revision_row, secret = await resolve_connection_revision(db, primary_search=True)
            _, provider = await web_search(
                provider=connection.connection_type, endpoint=revision_row.endpoint,
                api_key=secret, query="OpenAI", count=1, freshness=None, country=None,
                config=revision_row.config,
            )
            return {"ok": True, "provider": provider["name"], "latency_ms": provider["latency_ms"], "provider_error_code": None}
        except (ToolConnectionError, WebToolError) as error:
            return {"ok": False, "provider": None, "latency_ms": None, "provider_error_code": getattr(error, "code", type(error).__name__)}
    raise HTTPException(
        status_code=422,
        detail={
            "code": "tool_has_no_connection_test",
            "message": (
                "This Tool has no independent external connection. Test its "
                "typed ToolConnection or execute its deterministic contract gate."
            ),
        },
    )


@router.get("/tool-definitions/{tool_id}/versions")
async def list_tool_versions(
    tool_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    rows = list(await db.scalars(select(ToolVersion).where(
        ToolVersion.definition_id == tool_id,
    ).order_by(ToolVersion.version_no.desc())))
    return {"items": [{
        "id": item.id, "version_no": item.version_no,
        "input_schema": item.input_schema, "output_schema": item.output_schema,
        "examples": item.examples, "error_catalog": item.error_catalog,
        "annotations": item.annotations, "implementation_ref": item.implementation_ref,
        "implementation_version": item.implementation_version,
        "execution_mode": item.execution_mode, "side_effect": item.side_effect,
        "content_hash": item.content_hash, "published_at": item.published_at.isoformat(),
    } for item in rows]}


@router.get("/tool-connections")
async def list_tool_connections(
    connection_type: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    query = select(ToolConnection)
    if connection_type:
        query = query.where(ToolConnection.connection_type == connection_type)
    rows = list(await db.scalars(query.order_by(ToolConnection.connection_type, ToolConnection.connection_key)))
    items = []
    for connection in rows:
        revision_row = await db.get(ToolConnectionRevision, connection.current_revision_id) if connection.current_revision_id else None
        items.append(connection_view(connection, revision_row, admin=True))
    return {"items": items}


async def _upsert_connection(
    db: AsyncSession, current: CurrentAccount, body: ToolConnectionUpsert | ToolConnectionPatch,
    *, connection: ToolConnection | None,
) -> dict:
    try:
        connection, revision_row, _ = await create_or_update_connection(
            db, connection=connection,
            connection_key=(body.connection_key if isinstance(body, ToolConnectionUpsert) else connection.connection_key),
            display_name=body.display_name,
            connection_type=(body.connection_type if isinstance(body, ToolConnectionUpsert) else connection.connection_type),
            endpoint=body.endpoint, auth_type=body.auth_type, api_key=body.api_key,
            config=body.config, enabled=body.enabled, is_primary=body.is_primary,
            actor_account_id=current.account.id,
        )
    except ToolConnectionError as error:
        raise HTTPException(status_code=422, detail={"code": "invalid_tool_connection", "message": str(error)}) from error
    return connection_view(connection, revision_row, admin=True)


@router.post("/tool-connections", status_code=201)
async def create_tool_connection(
    body: ToolConnectionUpsert,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    request = body.model_dump(mode="json")
    if body.api_key:
        request["api_key"] = hashlib.sha256(body.api_key.encode()).hexdigest()
    record, replay = await idem(db, current, "create_tool_connection", idempotency_key, request)
    if replay and record.response_snapshot:
        return record.response_snapshot
    if await db.scalar(select(ToolConnection.id).where(ToolConnection.connection_key == body.connection_key)):
        raise HTTPException(status_code=409, detail={"code": "tool_connection_key_exists"})
    response = await _upsert_connection(db, current, body, connection=None)
    record.resource_type, record.resource_id, record.http_status = "tool_connection", response["id"], 201
    record.response_snapshot = response
    await db.commit()
    return response


@router.patch("/tool-connections/{connection_id}")
async def patch_tool_connection(
    connection_id: str, body: ToolConnectionPatch,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    request = body.model_dump(mode="json")
    if body.api_key:
        request["api_key"] = hashlib.sha256(body.api_key.encode()).hexdigest()
    record, replay = await idem(db, current, "patch_tool_connection", idempotency_key, {"id": connection_id, **request})
    if replay and record.response_snapshot:
        return record.response_snapshot
    connection = await db.scalar(select(ToolConnection).where(
        ToolConnection.id == connection_id,
    ).with_for_update())
    if connection is None:
        raise HTTPException(status_code=404, detail="Tool connection not found")
    if connection.version != revision(if_match):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "current_version": connection.version})
    response = await _upsert_connection(db, current, body, connection=connection)
    record.resource_type, record.resource_id, record.http_status = "tool_connection", connection.id, 200
    record.response_snapshot = response
    await db.commit()
    return response


@router.post("/tool-connections/{connection_id}/connection-test")
async def test_tool_connection(
    connection_id: str, db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    connection = await db.get(ToolConnection, connection_id)
    revision_row = await db.get(ToolConnectionRevision, connection.current_revision_id) if connection else None
    if connection is None or revision_row is None:
        raise HTTPException(status_code=404, detail="Tool connection not found")
    started = time.perf_counter()
    ok = False
    error_code = None
    provider = connection.connection_type
    details = {}
    try:
        if connection.connection_type in {"bocha", "brave", "bailian"}:
            _, _, secret = await resolve_connection_revision(db, revision_id=revision_row.id)
            items, provider_info = await web_search(
                provider=connection.connection_type, endpoint=revision_row.endpoint,
                api_key=secret, query="OpenAI", count=1, freshness=None, country=None,
                config=revision_row.config,
            )
            details = {
                "result_count": len(items),
                "request_id": provider_info.get("request_id"),
                **({
                    "model": provider_info.get("model"),
                    "search_strategy": provider_info.get("search_strategy"),
                    "usage": provider_info.get("usage", {}),
                } if connection.connection_type == "bailian" else {}),
            }
        elif connection.connection_type == "mcp":
            server, tools = await mcp_initialize_and_list(db, connection=connection, revision=revision_row)
            details = {"server": server, "tool_count": len(tools)}
        elif connection.connection_type == "browser":
            import redis.asyncio as aioredis
            from app.config import settings
            client = aioredis.from_url(settings.redis_url, decode_responses=True)
            try:
                heartbeat = await client.get("myclaw:browser-worker:heartbeat")
            finally:
                await client.aclose()
            if not heartbeat:
                raise RuntimeError("BrowserWorkerUnavailable")
            details = json.loads(heartbeat)
            if not details.get("ready") or details.get("engine") != "chromium":
                raise RuntimeError("BrowserWorkerNotReady")
        ok = True
    except Exception as error:
        error_code = getattr(error, "code", type(error).__name__)
    latency = round((time.perf_counter() - started) * 1000)
    connection.health_status = "healthy" if ok else "unhealthy"
    connection.health_checked_at = utcnow()
    connection.health_latency_ms = latency
    connection.health_error_code = error_code
    connection.version += 1
    await db.commit()
    return {"ok": ok, "provider": provider, "latency_ms": latency, "provider_error_code": error_code, "details": details}


@router.post("/mcp-servers/{connection_id}/sync")
async def sync_mcp_server(
    connection_id: str,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_account),
):
    require_admin(current)
    record, replay = await idem(db, current, "sync_mcp_server", idempotency_key, {"id": connection_id})
    if replay and record.response_snapshot:
        return record.response_snapshot
    connection = await db.get(ToolConnection, connection_id)
    if connection is None or connection.connection_type != "mcp":
        raise HTTPException(status_code=404, detail="MCP Server not found")
    try:
        response = await sync_mcp_tools(db, connection=connection, actor_account_id=current.account.id)
    except McpToolError as error:
        raise HTTPException(status_code=502, detail={"code": error.code, "message": str(error)}) from error
    record.resource_type, record.resource_id, record.http_status = "mcp_server", connection.id, 200
    record.response_snapshot = response
    await db.commit()
    return response


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
