"""Opaque, signed references for model-facing Platform principals.

Models may choose among references issued by the Runtime, but they never get
authority merely by inventing an account identifier.  Every consumer must
decode the reference in the current Tenant and Agent boundary and then repeat
the live access check before creating a cross-user action.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from app.config import settings


class InvalidPrincipalRef(ValueError):
    pass


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    if not value or "=" in value:
        raise InvalidPrincipalRef("invalid principal reference")
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True,
    )
    if _encode(decoded) != value:
        raise InvalidPrincipalRef("invalid principal reference")
    return decoded


def _signature(payload: bytes) -> bytes:
    return hmac.new(settings.secret_key.encode(), payload, hashlib.sha256).digest()


def encode_account_ref(*, tenant_id: str, agent_id: str, account_id: str) -> str:
    payload = json.dumps({
        "v": 1,
        "kind": "platform_account",
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "account_id": account_id,
    }, sort_keys=True, separators=(",", ":")).encode()
    return f"usr_{_encode(payload)}.{_encode(_signature(payload))}"


def decode_account_ref(value: str, *, tenant_id: str, agent_id: str) -> str:
    try:
        if not value.startswith("usr_"):
            raise InvalidPrincipalRef("invalid principal reference")
        encoded_payload, encoded_signature = value[4:].split(".", 1)
        payload = _decode(encoded_payload)
        supplied = _decode(encoded_signature)
        if not hmac.compare_digest(supplied, _signature(payload)):
            raise InvalidPrincipalRef("invalid principal reference")
        decoded = json.loads(payload)
        if (
            decoded.get("v") != 1
            or decoded.get("kind") != "platform_account"
            or decoded.get("tenant_id") != tenant_id
            or decoded.get("agent_id") != agent_id
            or not isinstance(decoded.get("account_id"), str)
        ):
            raise InvalidPrincipalRef("principal reference is outside the current boundary")
        return decoded["account_id"]
    except InvalidPrincipalRef:
        raise
    except Exception as error:
        raise InvalidPrincipalRef("invalid principal reference") from error
