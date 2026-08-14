"""Agent API key creation, authentication and revocation tests."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.agent_v1 import AgentBlueprint
from app.models.common import uuid7
from app.models.foundation import AgentApiKey, PlatformAccount, PlatformAuditEvent, UserIdentity
from app.models.tenant_v1 import Tenant
from app.services.agent_api_key_service import authenticate_api_key, create_api_key, revoke_api_key

pytestmark = pytest.mark.unit


@pytest.fixture
async def api_key_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Tenant.__table__,
        UserIdentity.__table__, PlatformAccount.__table__, PlatformAuditEvent.__table__,
        AgentBlueprint.__table__, AgentApiKey.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, agent_id = uuid7(), uuid7()
    async with factory.begin() as db:
        db.add(Tenant(id="test-tenant", slug="test", name="Test", status="active"))
        db.add(UserIdentity(user_id="builder", status="active"))
        db.add(PlatformAccount(
            id=account_id, tenant_id="test-tenant", user_id="builder",
            display_name="Builder", status="active",
        ))
        db.add(AgentBlueprint(
            id=agent_id, tenant_id="test-tenant", owner_account_id=account_id,
            visibility="private", status="draft",
        ))
    yield factory, account_id, agent_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_plaintext_is_returned_once_and_hash_authenticates(api_key_db):
    factory, account_id, agent_id = api_key_db
    async with factory.begin() as db:
        key, plaintext = await create_api_key(
            db, tenant_id="test-tenant", agent_id=agent_id,
            name="Production", caller_label="ERP",
            created_by_account_id=account_id, expires_at=None,
            idempotency_record_id="create-1",
        )
        assert plaintext.startswith(f"myclaw_{key.prefix}_")
        assert plaintext not in key.secret_hash

    async with factory.begin() as db:
        authenticated = await authenticate_api_key(db, plaintext)
        assert authenticated is not None
        assert authenticated.id == key.id
        assert authenticated.last_used_at is not None
        assert await authenticate_api_key(db, plaintext + "tampered") is None


@pytest.mark.asyncio
async def test_revocation_blocks_future_authentication(api_key_db):
    factory, account_id, agent_id = api_key_db
    async with factory.begin() as db:
        key, plaintext = await create_api_key(
            db, tenant_id="test-tenant", agent_id=agent_id,
            name="Integration", caller_label="CRM",
            created_by_account_id=account_id, expires_at=None,
            idempotency_record_id="create-2",
        )
    async with factory.begin() as db:
        revoked = await revoke_api_key(
            db, key_id=key.id, actor_account_id=account_id,
            idempotency_record_id="revoke-2",
        )
        assert revoked.status == "revoked"
    async with factory.begin() as db:
        assert await authenticate_api_key(db, plaintext) is None
