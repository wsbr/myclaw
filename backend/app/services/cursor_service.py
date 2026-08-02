"""Principal- and filter-bound opaque cursor pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Sequence

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError("non-canonical base64url")
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True,
    )
    if _b64encode(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"unsupported cursor value: {type(value)!r}")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=_json_default,
    ).encode()


def _signature(payload: bytes) -> bytes:
    return hmac.new(settings.secret_key.encode(), payload, hashlib.sha256).digest()


def encode_cursor(
    *, principal: str, resource: str, filters: dict[str, Any], values: Sequence[Any],
) -> str:
    payload = _canonical({
        "v": 1,
        "principal": principal,
        "resource": resource,
        "filters_hash": hashlib.sha256(_canonical(filters)).hexdigest(),
        "values": list(values),
    })
    return f"{_b64encode(payload)}.{_b64encode(_signature(payload))}"


def decode_cursor(
    value: str, *, principal: str, resource: str, filters: dict[str, Any],
) -> list[Any]:
    try:
        encoded_payload, encoded_signature = value.split(".", 1)
        payload = _b64decode(encoded_payload)
        supplied = _b64decode(encoded_signature)
        if not hmac.compare_digest(supplied, _signature(payload)):
            raise ValueError("cursor signature mismatch")
        decoded = json.loads(payload)
        expected_filters = hashlib.sha256(_canonical(filters)).hexdigest()
        if (
            decoded.get("v") != 1
            or decoded.get("principal") != principal
            or decoded.get("resource") != resource
            or decoded.get("filters_hash") != expected_filters
            or not isinstance(decoded.get("values"), list)
        ):
            raise ValueError("cursor context mismatch")
        return decoded["values"]
    except Exception as error:
        raise HTTPException(status_code=422, detail={"code": "invalid_cursor"}) from error


@dataclass(frozen=True)
class SortKey:
    column: Any
    descending: bool = True


def _coerce(column: Any, value: Any) -> Any:
    try:
        python_type = column.type.python_type
    except (AttributeError, NotImplementedError):
        return value
    if value is None or isinstance(value, python_type):
        return value
    if python_type is datetime:
        return datetime.fromisoformat(str(value))
    if python_type is date:
        return date.fromisoformat(str(value))
    if python_type is bool:
        return str(value).lower() in {"1", "true"}
    return python_type(value)


async def paginate_scalars(
    db: AsyncSession,
    statement: Any,
    *,
    sort: Sequence[SortKey],
    limit: int,
    cursor: str | None,
    principal: str,
    resource: str,
    filters: dict[str, Any],
) -> tuple[list[Any], str | None]:
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
    if not sort:
        raise ValueError("cursor pagination requires a stable sort")
    if cursor:
        raw_values = decode_cursor(
            cursor, principal=principal, resource=resource, filters=filters,
        )
        if len(raw_values) != len(sort):
            raise HTTPException(status_code=422, detail={"code": "invalid_cursor"})
        values = [_coerce(key.column, value) for key, value in zip(sort, raw_values)]
        branches = []
        for index, (key, value) in enumerate(zip(sort, values)):
            equal_prefix = [sort[pos].column == values[pos] for pos in range(index)]
            comparison = key.column < value if key.descending else key.column > value
            branches.append(and_(*equal_prefix, comparison))
        statement = statement.where(or_(*branches))
    order = [key.column.desc() if key.descending else key.column.asc() for key in sort]
    rows = list(await db.scalars(statement.order_by(*order).limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(
            principal=principal,
            resource=resource,
            filters=filters,
            values=[getattr(last, key.column.key) for key in sort],
        )
    return rows, next_cursor
