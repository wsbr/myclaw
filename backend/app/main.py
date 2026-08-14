from contextlib import asynccontextmanager
from http import HTTPStatus
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.config import settings
from app.database import engine
from app.models import *  # noqa: F401, F403 — ensures all models registered
from app.api.router import api_router
from app.models.common import uuid7
from app.core.rate_limit import consume, request_identifier
from app.services.registry_reconcile import (
    builtin_tool_specs,
    reconcile_platform_tools,
    seed_core_registries,
)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    rate_limit_redis = None
    if settings.rate_limit_enabled:
        import redis.asyncio as aioredis

        rate_limit_redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        app.state.rate_limit_redis = rate_limit_redis
    await _init_admin()
    await _init_phase_one_registries()
    try:
        yield
    finally:
        from app.services.knowledge_service import close_knowledge_retrieval_clients

        await close_knowledge_retrieval_clients()
        if rate_limit_redis is not None:
            await rate_limit_redis.aclose()


async def _init_admin():
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.foundation import PlatformAccount, PlatformAccountRole, UserIdentity
    from app.models.tenant_v1 import (
        DEFAULT_TENANT_ID, DEFAULT_TENANT_SLUG, Tenant, TenantMembership,
    )
    from app.models.common import utcnow, uuid7
    from app.core.security import hash_password

    async with AsyncSessionLocal.begin() as db:
        tenant = await db.get(Tenant, DEFAULT_TENANT_ID)
        if tenant is None:
            tenant = Tenant(
                id=DEFAULT_TENANT_ID, slug=DEFAULT_TENANT_SLUG,
                name="Default Tenant", status="active", version=1,
            )
            db.add(tenant)
            await db.flush()
        account = await db.scalar(select(PlatformAccount).where(
            PlatformAccount.email == settings.init_admin_email,
        ))
        if account is None:
            account_id = uuid7()
            db.add(UserIdentity(user_id=account_id, status="active"))
            await db.flush()
            account = PlatformAccount(
                id=account_id, tenant_id=tenant.id, user_id=account_id,
                email=settings.init_admin_email,
                display_name="admin", password_hash=hash_password(settings.init_admin_password),
                status="active", auth_revision=1, version=1,
            )
            db.add(account)
            await db.flush()
        for role in ("user", "admin"):
            if await db.get(PlatformAccountRole, (account.id, role)) is None:
                db.add(PlatformAccountRole(
                    account_id=account.id, role_key=role,
                    granted_by_account_id=account.id if role == "admin" else None,
                    granted_at=utcnow(),
                ))
        membership = await db.scalar(select(TenantMembership).where(
            TenantMembership.tenant_id == account.tenant_id,
            TenantMembership.account_id == account.id,
        ))
        if membership is None:
            db.add(TenantMembership(
                id=uuid7(), tenant_id=account.tenant_id, account_id=account.id,
                role_key="admin", status="active", version=1,
            ))


async def _init_phase_one_registries():
    """Idempotently align code registries after migrations have completed."""

    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal.begin() as db:
        await seed_core_registries(db)
        await reconcile_platform_tools(
            db,
            builtin_tool_specs(),
            job_run_id=uuid7(),
        )


app = FastAPI(
    title="MyClaw Agent Platform",
    version="0.4.1",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


def _error_response(
    request: Request, *, status_code: int, code: str, message: str,
    retryable: bool = False, details: dict | list | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", uuid7())
    response = JSONResponse(
        status_code=status_code,
        content={"error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "retryable": retryable,
            "details": details or {},
        }},
    )
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def request_identity(request: Request, call_next):
    request.state.request_id = uuid7()
    decision = None
    rate_limit_degraded = False
    if (
        settings.rate_limit_enabled
        and request.method != "OPTIONS"
        and request.url.path not in {"/health", "/openapi.json", "/docs", "/redoc"}
    ):
        identifier = request_identifier(request)
        authenticated = bool(request.headers.get("authorization", "").strip())
        limit = (
            settings.rate_limit_requests
            if authenticated else settings.rate_limit_unauthenticated_requests
        )
        try:
            decision = await consume(
                request.app.state.rate_limit_redis,
                identifier=identifier,
                secret=settings.secret_key,
                limit=limit,
                window_seconds=settings.rate_limit_window_seconds,
            )
        except Exception:
            rate_limit_degraded = True
            logger.warning(
                "rate limit unavailable; request admitted in degraded mode",
                extra={"request_id": request.state.request_id, "path": request.url.path},
            )
        if decision is not None and not decision.allowed:
            response = _error_response(
                request, status_code=429, code="rate_limited",
                message="Request rate limit exceeded", retryable=True,
                details={"retry_after_seconds": decision.retry_after},
            )
            response.headers["Retry-After"] = str(decision.retry_after)
            response.headers["X-RateLimit-Limit"] = str(decision.limit)
            response.headers["X-RateLimit-Remaining"] = "0"
            response.headers["X-RateLimit-Reset"] = str(decision.reset_epoch)
            return response
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    if decision is not None:
        response.headers["X-RateLimit-Limit"] = str(decision.limit)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        response.headers["X-RateLimit-Reset"] = str(decision.reset_epoch)
    elif rate_limit_degraded:
        response.headers["X-RateLimit-State"] = "degraded"
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, error: HTTPException):
    detail = error.detail
    if isinstance(detail, dict):
        code = str(detail.get("code") or f"http_{error.status_code}")
        message = str(detail.get("message") or HTTPStatus(error.status_code).phrase)
        details = {key: value for key, value in detail.items() if key not in {"code", "message"}}
    else:
        code = {
            401: "authentication_required", 403: "authorization_denied",
            404: "not_found", 409: "state_conflict", 410: "resource_gone",
            429: "rate_limited", 503: "dependency_unavailable",
        }.get(error.status_code, f"http_{error.status_code}")
        message = str(detail or HTTPStatus(error.status_code).phrase)
        details = {}
    return _error_response(
        request, status_code=error.status_code, code=code, message=message,
        retryable=error.status_code in {429, 502, 503, 504}, details=details,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    return _error_response(
        request, status_code=422, code="validation_error",
        message="Request validation failed", details={"issues": error.errors()},
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, error: Exception):
    del error
    return _error_response(
        request, status_code=500, code="internal_error",
        message="An unexpected server error occurred", retryable=True,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
