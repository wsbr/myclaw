"""Decode gzip-compressed span fields returned by the API."""

from __future__ import annotations

import base64
import gzip
import json
from typing import Any


def decode_span_field(raw: Any) -> dict | None:
    """Decode snapshot/request/response from API JSON (bytes as base64 or raw bytes)."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = base64.b64decode(raw)
        except Exception:
            return {"raw": raw}
    elif isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    else:
        return {"raw": raw}

    try:
        return json.loads(gzip.decompress(data))
    except Exception:
        try:
            return json.loads(data)
        except Exception:
            return {"raw": str(raw)}


def decode_spans(spans: list[dict]) -> list[dict]:
    decoded = []
    for span in spans:
        item = dict(span)
        for field in ("snapshot", "request", "response"):
            if field in item:
                item[f"{field}_decoded"] = decode_span_field(item[field])
        decoded.append(item)
    return decoded
