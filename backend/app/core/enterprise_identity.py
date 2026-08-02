"""Verification boundary for deployment-provided enterprise identity assertions."""

from __future__ import annotations

from jose import JWTError, jwt

from app.config import settings


class EnterpriseIdentityUnavailable(RuntimeError):
    pass


class InvalidEnterpriseAssertion(ValueError):
    pass


def verify_enterprise_assertion(assertion: str, *, audience: str) -> dict:
    secret = settings.enterprise_assertion_secret
    if not secret:
        raise EnterpriseIdentityUnavailable("Enterprise identity is not configured")
    options = {"require_aud": True, "require_exp": True, "require_sub": False}
    try:
        claims = jwt.decode(
            assertion,
            secret,
            algorithms=["HS256"],
            audience=audience,
            issuer=settings.enterprise_assertion_issuer or None,
            options=options,
        )
    except JWTError as error:
        raise InvalidEnterpriseAssertion("Enterprise identity assertion is invalid") from error
    user_id = claims.get("user_id") or claims.get("sub")
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 191:
        raise InvalidEnterpriseAssertion("Enterprise assertion has no valid user_id")
    return {**claims, "user_id": user_id.strip()}
