"""Negative proofs for the minimum Tenant authority boundary."""

from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.agents import _get_accessible_agent
from app.api.artifacts import _owned_artifact
from app.api.groups import owned_group
from app.api.memories import _owned_memory
from app.api.runs import _owned_run
from app.api.sessions import _owned_session
from app.api.usage import _entry_query
from app.core.security import create_access_token
from app.database import Base
from app.deps import CurrentAccount, SessionActor, _resolve_account
from app.models.agent_v1 import AgentBlueprint
from app.models.artifact_v1 import Artifact
from app.models.common import utcnow
from app.models.foundation import (
    OutboxCommand,
    PlatformAccount,
    PlatformAccountRole,
    UserIdentity,
    UserSession,
)
from app.models.group_v1 import GroupSession
from app.models.memory_v1 import MemoryHead, MemoryRecord
from app.models.runtime_v1 import Run
from app.models.tenant_v1 import Tenant, TenantMembership
from app.models.usage_v1 import UsageLedgerEntry
from app.persistence.reliable_writes import tenant_command_error


pytestmark = pytest.mark.unit

TENANT_A = "01b00000-0000-7000-8000-000000000001"
TENANT_B = "01b00000-0000-7000-8000-000000000002"
ACCOUNT_A = "01b00000-0000-7000-8000-000000000011"
ACCOUNT_B = "01b00000-0000-7000-8000-000000000012"
AGENT_A = "01b00000-0000-7000-8000-000000000021"
SESSION_A = "01b00000-0000-7000-8000-000000000031"
RUN_A = "01b00000-0000-7000-8000-000000000041"
GROUP_A = "01b00000-0000-7000-8000-000000000051"
MEMORY_A = "01b00000-0000-7000-8000-000000000061"
ARTIFACT_A = "01b00000-0000-7000-8000-000000000071"


@pytest.fixture
async def tenant_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Tenant.__table__,
        UserIdentity.__table__,
        PlatformAccount.__table__,
        PlatformAccountRole.__table__,
        TenantMembership.__table__,
        AgentBlueprint.__table__,
        UserSession.__table__,
        Run.__table__,
        GroupSession.__table__,
        MemoryHead.__table__,
        MemoryRecord.__table__,
        Artifact.__table__,
        UsageLedgerEntry.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as db:
        db.add_all([
            Tenant(id=TENANT_A, slug="alpha", name="Alpha", status="active"),
            Tenant(id=TENANT_B, slug="beta", name="Beta", status="active"),
            UserIdentity(user_id="alice", status="active"),
            UserIdentity(user_id="bob", status="active"),
            PlatformAccount(
                id=ACCOUNT_A, tenant_id=TENANT_A, user_id="alice",
                display_name="Alice", status="active", auth_revision=1,
            ),
            PlatformAccount(
                id=ACCOUNT_B, tenant_id=TENANT_B, user_id="bob",
                display_name="Bob", status="active", auth_revision=1,
            ),
            PlatformAccountRole(
                account_id=ACCOUNT_A, role_key="admin", granted_at=utcnow(),
            ),
            PlatformAccountRole(
                account_id=ACCOUNT_B, role_key="admin", granted_at=utcnow(),
            ),
            TenantMembership(
                tenant_id=TENANT_A, account_id=ACCOUNT_A,
                role_key="admin", status="active",
            ),
            TenantMembership(
                tenant_id=TENANT_B, account_id=ACCOUNT_B,
                role_key="admin", status="active",
            ),
            AgentBlueprint(
                id=AGENT_A, tenant_id=TENANT_A, owner_account_id=ACCOUNT_A,
                visibility="enterprise", status="published",
            ),
            UserSession(
                id=SESSION_A, tenant_id=TENANT_A, account_id=ACCOUNT_A,
                user_id="alice", entry_agent_id=AGENT_A, status="active",
            ),
            Run(
                id=RUN_A, tenant_id=TENANT_A, account_id=ACCOUNT_A,
                session_id=SESSION_A, user_id="alice", mode="chat",
                status="active", permission_mode="direct",
            ),
            GroupSession(
                id=GROUP_A, tenant_id=TENANT_A, session_id=SESSION_A,
                user_id="alice", initiator_agent_id=AGENT_A, status="open",
                formation_mode="emergent_model_selected",
            ),
            MemoryHead(
                id="01b00000-0000-7000-8000-000000000062",
                tenant_id=TENANT_A, user_id="alice", scope="user",
                fact_key="profile.codename",
            ),
            MemoryRecord(
                id=MEMORY_A,
                tenant_id=TENANT_A,
                head_id="01b00000-0000-7000-8000-000000000062",
                user_id="alice", scope="user", fact_key="profile.codename",
                content="Falcon", normalized_hash="a" * 64,
                status="active", source_kind="user_edit",
            ),
            Artifact(
                id=ARTIFACT_A, tenant_id=TENANT_A, user_id="alice",
                session_id=SESSION_A,
                workspace_id="01b00000-0000-7000-8000-000000000081",
                relative_path=f".myclaw/artifacts/{ARTIFACT_A}/proof.txt",
                name="proof.txt", mime_type="text/plain", size_bytes=5,
                checksum="b" * 64, publication_key="c" * 64,
                created_by_agent_id=AGENT_A, status="published", version_no=1,
            ),
            UsageLedgerEntry(
                id="01b00000-0000-7000-8000-000000000091",
                tenant_id=TENANT_A, account_id=ACCOUNT_A,
                session_id=SESSION_A, run_id=RUN_A,
                source_type="adjustment",
                source_id="01b00000-0000-7000-8000-000000000092",
                source_operation_key="tenant-alpha-adjustment",
                metric="tokens", unit="token", quantity=Decimal("1"),
                measurement_status="measured", cost_status="unknown",
            ),
        ])
    yield factory
    await engine.dispose()


async def _tenant_b_actor(db):
    account = await db.get(PlatformAccount, ACCOUNT_B)
    return SessionActor(account=account, roles=frozenset({"admin"}))


@pytest.mark.asyncio
async def test_guessed_cross_tenant_ids_are_indistinguishable_from_missing(tenant_db):
    async with tenant_db() as db:
        actor = await _tenant_b_actor(db)
        current = CurrentAccount(account=actor.account, roles=actor.roles)
        calls = (
            _owned_session(db, SESSION_A, actor),
            _owned_run(db, RUN_A, actor),
            owned_group(db, GROUP_A, actor),
            _owned_artifact(db, ARTIFACT_A, actor),
            _owned_memory(db, MEMORY_A, current),
            _get_accessible_agent(db, AGENT_A, current),
        )
        for call in calls:
            with pytest.raises(HTTPException) as caught:
                await call
            assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_tenant_admin_usage_query_cannot_cross_boundary(tenant_db):
    async with tenant_db() as db:
        rows = list(await db.scalars(_entry_query(
            tenant_id=TENANT_B, run_id=None, account_id=None,
            agent_version_id=None, model_version_id=None,
        )))
        assert rows == []
        assert len(list(await db.scalars(_entry_query(
            tenant_id=TENANT_A, run_id=None, account_id=None,
            agent_version_id=None, model_version_id=None,
        )))) == 1


@pytest.mark.asyncio
async def test_token_tenant_claim_and_membership_are_revalidated(tenant_db):
    async with tenant_db.begin() as db:
        account = await db.get(PlatformAccount, ACCOUNT_B)
        stale = create_access_token(account.id, claims={
            "account_id": account.id,
            "tenant_id": TENANT_A,
            "user_id": account.user_id,
            "auth_revision": account.auth_revision,
        })
        with pytest.raises(HTTPException) as caught:
            await _resolve_account(stale, db)
        assert caught.value.status_code == 401

        membership = await db.scalar(select(TenantMembership).where(
            TenantMembership.account_id == ACCOUNT_B,
        ))
        membership.status = "disabled"
        valid_claim = create_access_token(account.id, claims={
            "account_id": account.id,
            "tenant_id": TENANT_B,
            "user_id": account.user_id,
            "auth_revision": account.auth_revision,
        })
        with pytest.raises(HTTPException) as caught:
            await _resolve_account(valid_claim, db)
        assert caught.value.status_code == 401


def test_outbox_authority_envelope_fails_closed():
    command = OutboxCommand(
        topic="runtime.runner", action="execute", aggregate_type="runner",
        aggregate_id="runner-a", partition_key=SESSION_A, dedupe_key="runner-a",
        tenant_id=TENANT_A, account_id=ACCOUNT_A,
    )
    assert tenant_command_error(
        command, tenant_id=TENANT_A, account_id=ACCOUNT_A,
    ) is None
    assert tenant_command_error(
        command, tenant_id=TENANT_B, account_id=ACCOUNT_B,
    ) == "tenant_authority_mismatch"
    command.tenant_id = None
    assert tenant_command_error(
        command, tenant_id=TENANT_A, account_id=ACCOUNT_A,
    ) == "tenant_authority_missing"
