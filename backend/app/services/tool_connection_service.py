"""Immutable Tool connection revision resolution without secret projection."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from sqlalchemy import select, update

from app.core.platform_secrets import decrypt_secret, store_secret
from app.models.common import utcnow, uuid7
from app.models.registry import (
    PlatformSecret, ToolConnection, ToolConnectionRevision,
)


class ToolConnectionError(ValueError):
    pass


def validate_connection_endpoint(value: str, *, connection_type: str) -> str:
    endpoint = (value or "").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https" or not parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise ToolConnectionError("Tool connection endpoint must be an absolute credential-free HTTPS URL")
    if len(endpoint) > 1000:
        raise ToolConnectionError("Tool connection endpoint is too long")
    if connection_type == "browser" and endpoint not in {"https://local-browser-worker"}:
        raise ToolConnectionError("Browser Worker endpoint is platform-managed")
    return endpoint


def _assert_public_config(value: object, *, path: str = "config") -> None:
    """Keep credential material out of immutable clear-text config snapshots."""

    sensitive = {
        "apikey", "api_key", "authorization", "bearer", "bearer_token",
        "credential", "credentials", "password", "secret", "token", "access_token",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            compact = normalized.replace("_", "")
            if normalized in sensitive or compact in sensitive:
                raise ToolConnectionError(
                    f"{path}.{key} may contain a credential; submit it through write-only api_key"
                )
            _assert_public_config(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_public_config(item, path=f"{path}[{index}]")


def _validate_connection_config(connection_type: str, config: dict) -> None:
    if connection_type != "bailian":
        return
    unknown = set(config) - {"model", "search_strategy"}
    if unknown:
        raise ToolConnectionError(
            "Bailian config contains unsupported fields: " + ", ".join(sorted(unknown))
        )
    model = config.get("model", "qwen-plus")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,190}", model):
        raise ToolConnectionError("Bailian config.model is invalid")
    strategy = config.get("search_strategy", "turbo")
    if strategy not in {"turbo", "max"}:
        raise ToolConnectionError("Bailian config.search_strategy must be turbo or max")


async def _assert_public_endpoint(endpoint: str, *, connection_type: str) -> None:
    if connection_type == "browser":
        return
    from app.services.web_tool_service import WebToolError, _resolve_public

    parsed = urlsplit(endpoint)
    port = parsed.port or 443
    try:
        await _resolve_public(parsed.hostname, port)
    except WebToolError as error:
        raise ToolConnectionError(str(error)) from error


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


async def create_or_update_connection(
    db,
    *,
    connection: ToolConnection | None,
    connection_key: str,
    display_name: str,
    connection_type: str,
    endpoint: str,
    auth_type: str,
    api_key: str | None,
    config: dict,
    enabled: bool,
    is_primary: bool,
    actor_account_id: str,
) -> tuple[ToolConnection, ToolConnectionRevision, bool]:
    endpoint = validate_connection_endpoint(endpoint, connection_type=connection_type)
    _assert_public_config(config)
    _validate_connection_config(connection_type, config)
    await _assert_public_endpoint(endpoint, connection_type=connection_type)
    required_auth = {
        "bocha": "bearer", "brave": "x_api_key",
        "bailian": "bearer", "browser": "none",
    }
    if connection_type in required_auth and auth_type != required_auth[connection_type]:
        raise ToolConnectionError(
            f"{connection_type} connections require auth_type={required_auth[connection_type]}"
        )
    search_providers = {"bocha", "brave", "bailian"}
    if is_primary and connection_type not in search_providers:
        raise ToolConnectionError("only a Search Provider connection may be primary")
    if auth_type == "none" and api_key:
        raise ToolConnectionError("anonymous connections must not include api_key")
    if auth_type != "none" and connection is None and not api_key:
        raise ToolConnectionError("api_key is required for an authenticated connection")
    if is_primary and connection_type in search_providers:
        # Serialize primary selection with all Search connection updates. This
        # keeps the provider switch atomic even when two administrators submit
        # conflicting requests at the same time.
        list(await db.scalars(
            select(ToolConnection.id).where(
                ToolConnection.connection_type.in_(search_providers),
            ).with_for_update()
        ))
    if connection is None:
        connection = ToolConnection(
            id=uuid7(), connection_key=connection_key, display_name=display_name,
            connection_type=connection_type, auth_type=auth_type,
            status="active" if enabled else "inactive", is_primary=is_primary,
            next_revision_no=1, health_status="untested",
            created_by_account_id=actor_account_id,
        )
        db.add(connection)
        await db.flush()
    elif connection.connection_type != connection_type:
        raise ToolConnectionError("connection_type is immutable")
    current = await db.get(ToolConnectionRevision, connection.current_revision_id) if connection.current_revision_id else None
    supplied_fingerprint = hashlib.sha256(api_key.encode()).hexdigest() if api_key else None
    retained_secret = (
        await db.get(PlatformSecret, current.secret_id)
        if auth_type != "none" and current and current.secret_id else None
    )
    secret_fingerprint = supplied_fingerprint or (
        retained_secret.fingerprint if retained_secret else None
    )
    if auth_type != "none" and not secret_fingerprint:
        raise ToolConnectionError("api_key is required for an authenticated connection")
    payload = {
        "endpoint": endpoint, "auth_type": auth_type,
        "secret_fingerprint": secret_fingerprint,
        "config": config,
    }
    content_hash = _hash(payload)
    revision = await db.scalar(select(ToolConnectionRevision).where(
        ToolConnectionRevision.connection_id == connection.id,
        ToolConnectionRevision.content_hash == content_hash,
    ))
    created = revision is None
    if revision is None:
        secret = retained_secret
        if api_key:
            secret = await store_secret(
                db, plaintext=api_key, purpose=f"tool-connection:{connection.id}",
                created_by_account_id=actor_account_id,
                rotated_from_id=current.secret_id if current else None,
            )
        revision = ToolConnectionRevision(
            id=uuid7(), connection_id=connection.id,
            revision_no=connection.next_revision_no, endpoint=endpoint,
            auth_type=auth_type, secret_id=secret.id if secret else None,
            secret_fingerprint=secret.fingerprint if secret else None,
            config=config, content_hash=content_hash,
            created_by_account_id=actor_account_id,
        )
        db.add(revision)
        await db.flush()
        connection.next_revision_no += 1
    connection.current_revision_id = revision.id
    connection.display_name = display_name
    connection.auth_type = auth_type
    connection.status = "active" if enabled else "inactive"
    connection.is_primary = is_primary
    connection.health_status = "untested"
    connection.health_checked_at = None
    connection.health_latency_ms = None
    connection.health_error_code = None
    connection.version += 1
    if is_primary and connection_type in search_providers:
        await db.execute(update(ToolConnection).where(
            ToolConnection.id != connection.id,
            ToolConnection.connection_type.in_(search_providers),
            ToolConnection.is_primary.is_(True),
        ).values(is_primary=False, version=ToolConnection.version + 1))
    await db.flush()
    return connection, revision, created


async def resolve_connection_revision(
    db, *, revision_id: str | None = None, primary_search: bool = False,
) -> tuple[ToolConnection, ToolConnectionRevision, str]:
    if revision_id:
        revision = await db.get(ToolConnectionRevision, revision_id)
        connection = await db.get(ToolConnection, revision.connection_id) if revision else None
    elif primary_search:
        connection = await db.scalar(select(ToolConnection).where(
            ToolConnection.connection_type.in_(["bocha", "brave", "bailian"]),
            ToolConnection.is_primary.is_(True), ToolConnection.status == "active",
        ))
        revision = await db.get(ToolConnectionRevision, connection.current_revision_id) if connection else None
    else:
        connection = revision = None
    if connection is None or revision is None or connection.status != "active":
        raise ToolConnectionError("the frozen Tool connection revision is unavailable")
    secret = ""
    if revision.secret_id:
        record = await db.get(PlatformSecret, revision.secret_id)
        if record is None or record.status == "revoked":
            raise ToolConnectionError("the frozen Tool secret is unavailable")
        if record.fingerprint != revision.secret_fingerprint:
            raise ToolConnectionError("Tool secret fingerprint mismatch")
        secret = decrypt_secret(record.ciphertext, purpose=record.purpose)
    return connection, revision, secret


def connection_view(connection: ToolConnection, revision: ToolConnectionRevision | None, *, admin: bool) -> dict:
    return {
        "id": connection.id,
        "connection_key": connection.connection_key,
        "display_name": connection.display_name,
        "connection_type": connection.connection_type,
        "auth_type": connection.auth_type,
        "status": connection.status,
        "enabled": connection.status == "active",
        "is_primary": connection.is_primary,
        "current_revision_id": connection.current_revision_id,
        "revision_no": revision.revision_no if revision else None,
        "endpoint": revision.endpoint if admin and revision else None,
        "secret_configured": bool(revision and revision.secret_id),
        "secret_fingerprint": revision.secret_fingerprint if revision else None,
        "config": dict(revision.config or {}) if revision else {},
        "health": {
            "status": connection.health_status,
            "checked_at": connection.health_checked_at.isoformat() if connection.health_checked_at else None,
            "latency_ms": connection.health_latency_ms,
            "error_code": connection.health_error_code,
        },
        "version": connection.version,
    }
