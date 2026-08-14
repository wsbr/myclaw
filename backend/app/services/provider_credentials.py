"""Credential resolution shared by provider clients without runtime imports."""

from __future__ import annotations

from app.core.platform_secrets import resolve_secret_reference


def resolve_credential(reference: str | None) -> str:
    del reference
    raise RuntimeError("model credentials must use the platform Secret store")


async def resolve_provider_credential(db, reference: str | None) -> str:
    if reference and reference.startswith("secret://"):
        return await resolve_secret_reference(db, reference)
    return resolve_credential(reference)
