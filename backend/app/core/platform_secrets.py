"""Generic write-only AES-GCM secret storage for provider connections.

Only opaque ``secret://`` references and SHA-256 fingerprints are allowed to
leave this module. Plaintext exists briefly at the HTTP/worker boundary and is
never included in API projections, idempotency snapshots, events, or traces.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.common import uuid7
from app.models.registry import PlatformSecret


SECRET_REFERENCE_PREFIX = "secret://"
ENCRYPTED_SECRET_PREFIX = "aesgcm:v1:"


class PlatformSecretError(ValueError):
    pass


def _key() -> bytes:
    configured = settings.encryption_key.strip()
    try:
        decoded = bytes.fromhex(configured)
    except ValueError:
        decoded = b""
    if len(decoded) == 32:
        return decoded
    return hashlib.sha256(configured.encode()).digest()


def encrypt_secret(plaintext: str, *, purpose: str) -> tuple[str, str]:
    if not isinstance(plaintext, str) or not plaintext.strip():
        raise PlatformSecretError("secret value is required")
    if len(plaintext) > 4096:
        raise PlatformSecretError("secret value is too long")
    if not purpose or len(purpose) > 100:
        raise PlatformSecretError("secret purpose is invalid")
    fingerprint = hashlib.sha256(plaintext.encode()).hexdigest()
    nonce = os.urandom(12)
    aad = f"myclaw:platform-secret:v1:{purpose}".encode()
    encrypted = AESGCM(_key()).encrypt(nonce, plaintext.encode(), aad)
    token = base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")
    return f"{ENCRYPTED_SECRET_PREFIX}{token}", fingerprint


def decrypt_secret(ciphertext: str, *, purpose: str) -> str:
    if not ciphertext.startswith(ENCRYPTED_SECRET_PREFIX):
        raise PlatformSecretError("secret ciphertext has an unsupported format")
    token = ciphertext.removeprefix(ENCRYPTED_SECRET_PREFIX)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if len(raw) <= 12:
            raise ValueError("ciphertext too short")
        aad = f"myclaw:platform-secret:v1:{purpose}".encode()
        value = AESGCM(_key()).decrypt(raw[:12], raw[12:], aad)
        return value.decode()
    except (InvalidTag, UnicodeDecodeError, ValueError) as error:
        raise PlatformSecretError("secret ciphertext failed integrity validation") from error


async def store_secret(
    db: AsyncSession,
    *,
    plaintext: str,
    purpose: str,
    created_by_account_id: str | None,
    rotated_from_id: str | None = None,
) -> PlatformSecret:
    ciphertext, fingerprint = encrypt_secret(plaintext, purpose=purpose)
    record = PlatformSecret(
        id=uuid7(), purpose=purpose, ciphertext=ciphertext,
        fingerprint=fingerprint, status="active",
        rotated_from_id=rotated_from_id,
        created_by_account_id=created_by_account_id,
    )
    db.add(record)
    if rotated_from_id:
        previous = await db.get(PlatformSecret, rotated_from_id)
        if previous is not None and previous.status == "active":
            previous.status = "rotated"
            previous.version += 1
    await db.flush()
    return record


async def resolve_secret_reference(db: AsyncSession, reference: str | None) -> str:
    if not reference:
        return ""
    if not reference.startswith(SECRET_REFERENCE_PREFIX):
        raise PlatformSecretError("only secret:// references are accepted")
    secret_id = reference.removeprefix(SECRET_REFERENCE_PREFIX)
    record = await db.get(PlatformSecret, secret_id)
    if record is None or record.status not in {"active", "rotated"}:
        raise PlatformSecretError("secret reference is unavailable")
    return decrypt_secret(record.ciphertext, purpose=record.purpose)


def secret_reference(record: PlatformSecret) -> str:
    return f"{SECRET_REFERENCE_PREFIX}{record.id}"


def safe_secret_view(record: PlatformSecret) -> dict:
    return {
        "configured": True,
        "fingerprint": record.fingerprint,
        "status": record.status,
        "version": record.version,
    }


def credential_source(reference: str | None) -> str | None:
    """Project only the provider-neutral storage class, never secret data."""

    if not reference:
        return None
    if reference.startswith(SECRET_REFERENCE_PREFIX):
        return "platform_secret"
    return "unsupported"
