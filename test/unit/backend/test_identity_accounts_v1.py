"""Canonical PlatformAccount authentication and three-role administration."""

import pytest
from fastapi import HTTPException
from jose import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.admin import (
    AccountCreate, AccountRolesPatch, AccountStatusPatch,
    create_account, patch_account_roles, patch_account_status,
)
from app.api.accounts import AccountProfilePatch, ChangeUserIdRequest, change_my_user_id, patch_my_account
from app.api.auth import enterprise_callback, login
from app.config import settings
from app.core.security import hash_password
from app.database import Base
from app.deps import CurrentAccount, _resolve_account
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    IdempotencyRecord, PlatformAccount, PlatformAccountRole,
    PlatformAuditEvent, UserIdentity,
)
from app.schemas.auth import LoginRequest


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_platform_account_login_roles_status_and_idempotent_audit():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, PlatformAccountRole.__table__,
        IdempotencyRecord.__table__, PlatformAuditEvent.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid7()
    async with factory.begin() as db:
        db.add(UserIdentity(user_id="admin-user", status="active"))
        admin = PlatformAccount(
            id=admin_id, user_id="admin-user", email="admin@example.com",
            display_name="Admin", password_hash=hash_password("admin-password"),
            status="active", auth_revision=1, version=1,
        )
        db.add(admin)
        await db.flush()
        db.add_all([
            PlatformAccountRole(account_id=admin_id, role_key="user", granted_at=utcnow()),
            PlatformAccountRole(account_id=admin_id, role_key="admin", granted_at=utcnow()),
        ])
    current = CurrentAccount(account=admin, roles=frozenset({"user", "admin"}))

    async with factory() as db:
        created = await create_account(
            AccountCreate(
                user_id="employee-42", email="builder@example.com", display_name="Builder",
                password="builder-password", roles=["user", "builder"],
            ),
            idempotency_key="create-builder", db=db, current=current,
        )
    assert created["user_id"] == "employee-42"
    assert created["roles"] == ["builder", "user"]

    async with factory() as db:
        replay = await create_account(
            AccountCreate(
                user_id="employee-42", email="builder@example.com", display_name="Builder",
                password="builder-password", roles=["user", "builder"],
            ),
            idempotency_key="create-builder", db=db, current=current,
        )
        assert replay["id"] == created["id"]
        assert await db.scalar(select(func.count()).select_from(PlatformAccount).where(
            PlatformAccount.user_id == "employee-42",
        )) == 1

    async with factory() as db:
        login_result = await login(LoginRequest(
            email="builder@example.com", password="builder-password",
        ), db=db)
        old_token = login_result.access_token
        assert login_result.user.role == "builder"

    async with factory() as db:
        roles_changed = await patch_account_roles(
            created["id"], AccountRolesPatch(roles=["user"]),
            if_match=str(created["version"]), idempotency_key="demote-builder",
            db=db, current=current,
        )
    assert roles_changed["roles"] == ["user"]
    assert roles_changed["version"] == created["version"] + 1

    async with factory() as db:
        with pytest.raises(HTTPException) as stale:
            await _resolve_account(old_token, db)
        assert stale.value.status_code == 401

    async with factory() as db:
        disabled = await patch_account_status(
            created["id"], AccountStatusPatch(status="disabled"),
            if_match=str(roles_changed["version"]), idempotency_key="disable-builder",
            db=db, current=current,
        )
    assert disabled["status"] == "disabled"

    async with factory() as db:
        with pytest.raises(HTTPException) as rejected:
            await login(LoginRequest(email="builder@example.com", password="builder-password"), db=db)
        assert rejected.value.status_code == 403
        actions = set(await db.scalars(select(PlatformAuditEvent.action)))
        assert actions == {
            "account.created", "account.login", "account.roles_updated", "account.status_updated",
        }
    await engine.dispose()


@pytest.mark.asyncio
async def test_self_service_profile_and_verified_user_id_change(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, PlatformAccountRole.__table__,
        IdempotencyRecord.__table__, PlatformAuditEvent.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id = uuid7()
    async with factory.begin() as db:
        db.add(UserIdentity(user_id="temporary-user", status="active"))
        account = PlatformAccount(
            id=account_id, user_id="temporary-user", email="member@example.com",
            display_name="Member", password_hash=hash_password("member-password"),
            status="active", auth_revision=1, version=1,
        )
        db.add(account)
        await db.flush()
        db.add(PlatformAccountRole(account_id=account_id, role_key="user", granted_at=utcnow()))
    current = CurrentAccount(account=account, roles=frozenset({"user"}))

    async with factory() as db:
        updated = await patch_my_account(
            AccountProfilePatch(display_name="Verified Member"),
            if_match='"1"', idempotency_key="profile-1", db=db, current=current,
        )
    assert updated["display_name"] == "Verified Member"
    assert updated["version"] == 2

    monkeypatch.setattr(settings, "enterprise_assertion_secret", "test-enterprise-secret")
    monkeypatch.setattr(settings, "enterprise_assertion_issuer", "test-idp")
    assertion = jwt.encode(
        {"user_id": "employee-verified", "aud": "myclaw-user-id-link", "iss": "test-idp", "exp": 4102444800},
        "test-enterprise-secret", algorithm="HS256",
    )
    async with factory() as db:
        changed = await change_my_user_id(
            ChangeUserIdRequest(enterprise_assertion=assertion),
            if_match='"2"', idempotency_key="identity-1", db=db, current=current,
        )
    assert changed["user_id"] == "employee-verified"
    assert changed["auth_revision"] == 2
    async with factory() as db:
        actions = set(await db.scalars(select(PlatformAuditEvent.action)))
        assert actions == {"account.profile_updated", "account.user_id_changed"}
        assert await db.get(UserIdentity, "temporary-user") is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_enterprise_callback_creates_only_basic_user(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, PlatformAccountRole.__table__,
        PlatformAuditEvent.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "enterprise_assertion_secret", "test-enterprise-secret")
    monkeypatch.setattr(settings, "enterprise_assertion_issuer", "test-idp")
    assertion = jwt.encode(
        {
            "user_id": "sso-employee", "email": "sso@example.com", "name": "SSO Employee",
            "aud": "myclaw-enterprise-login", "iss": "test-idp", "exp": 4102444800,
        },
        "test-enterprise-secret", algorithm="HS256",
    )
    async with factory() as db:
        result = await enterprise_callback(assertion=assertion, db=db)
    assert result.user.user_id == "sso-employee"
    assert result.user.roles == ["user"]
    async with factory() as db:
        assert set(await db.scalars(select(PlatformAccountRole.role_key))) == {"user"}
    await engine.dispose()
