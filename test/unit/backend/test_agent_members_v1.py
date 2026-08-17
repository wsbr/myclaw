"""Private Agent member whitelist authorization tests."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.agents import (
    _get_accessible_agent,
    _get_owned_agent,
    replace_agent_members,
)
from app.api.sessions import _owned_session, create_message
from app.database import Base
from app.deps import CurrentAccount
from app.deps import SessionActor
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint, AgentMember
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    IdempotencyRecord,
    PlatformAccount,
    PlatformAccountRole,
    PlatformAuditEvent,
    UserIdentity,
    UserSession,
)
from app.models.tenant_v1 import Tenant, TenantMembership
from app.schemas.agent_v1 import AgentMemberReplaceRequest
from app.schemas.runtime_v1 import MessageCreateRequest
from app.services.agent_access_service import agent_use_filter, has_agent_use_access


pytestmark = pytest.mark.unit


@pytest.fixture
async def member_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Tenant.__table__, UserIdentity.__table__, PlatformAccount.__table__,
        PlatformAccountRole.__table__, TenantMembership.__table__,
        AgentBlueprint.__table__, AgentAssemblyVersion.__table__, AgentMember.__table__,
        UserSession.__table__,
        IdempotencyRecord.__table__, PlatformAuditEvent.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, other_tenant_id = uuid7(), uuid7()
    owner_id, member_id, outsider_id = uuid7(), uuid7(), uuid7()
    agent_id, version_id, session_id = uuid7(), uuid7(), uuid7()
    now = utcnow()
    async with factory.begin() as db:
        db.add_all([
            Tenant(id=tenant_id, slug="primary", name="Primary", status="active"),
            Tenant(id=other_tenant_id, slug="other", name="Other", status="active"),
            UserIdentity(user_id="owner", status="active"),
            UserIdentity(user_id="member", status="active"),
            UserIdentity(user_id="outsider", status="active"),
        ])
        await db.flush()
        owner = PlatformAccount(
            id=owner_id, tenant_id=tenant_id, user_id="owner",
            email="owner@example.test", display_name="Owner", status="active",
        )
        member = PlatformAccount(
            id=member_id, tenant_id=tenant_id, user_id="member",
            email="member@example.test", display_name="Member", status="active",
        )
        outsider = PlatformAccount(
            id=outsider_id, tenant_id=other_tenant_id, user_id="outsider",
            email="outsider@example.test", display_name="Outsider", status="active",
        )
        db.add_all([owner, member, outsider])
        await db.flush()
        db.add_all([
            TenantMembership(
                id=uuid7(), tenant_id=tenant_id, account_id=owner_id,
                role_key="member", status="active",
            ),
            TenantMembership(
                id=uuid7(), tenant_id=tenant_id, account_id=member_id,
                role_key="member", status="active",
            ),
            TenantMembership(
                id=uuid7(), tenant_id=other_tenant_id, account_id=outsider_id,
                role_key="member", status="active",
            ),
            PlatformAccountRole(
                account_id=owner_id, role_key="builder",
                granted_by_account_id=owner_id, granted_at=now,
            ),
            PlatformAccountRole(
                account_id=owner_id, role_key="user",
                granted_by_account_id=owner_id, granted_at=now,
            ),
            PlatformAccountRole(
                account_id=member_id, role_key="user",
                granted_by_account_id=owner_id, granted_at=now,
            ),
        ])
        agent = AgentBlueprint(
            id=agent_id, tenant_id=tenant_id, owner_account_id=owner_id,
            visibility="private", status="published",
            draft_version_id=version_id,
            current_published_version_id=version_id, next_version_no=2,
        )
        db.add(agent)
        await db.flush()
        db.add(AgentAssemblyVersion(
            id=version_id, agent_id=agent_id, version_no=1,
            publication_status="published", name="Private reviewer",
            description="Review evidence", instructions="Review",
            group_enabled=True, discovery_config={}, collaboration_config={},
            created_by_account_id=owner_id, published_at=now,
            content_hash="a" * 64, immutable_at=now,
        ))
        db.add(UserSession(
            id=session_id, tenant_id=tenant_id, account_id=member_id,
            user_id="member", entry_agent_id=agent_id, status="active",
            origin_type="web", permission_mode="direct",
            permission_revision=1,
        ))
    yield factory, {
        "tenant_id": tenant_id, "owner_id": owner_id, "member_id": member_id,
        "outsider_id": outsider_id, "agent_id": agent_id,
        "session_id": session_id,
    }
    await engine.dispose()


@pytest.mark.asyncio
async def test_owner_can_grant_and_revoke_private_agent_use(member_db):
    factory, ids = member_db
    async with factory() as db:
        owner = await db.get(PlatformAccount, ids["owner_id"])
        member = await db.get(PlatformAccount, ids["member_id"])
        agent = await db.get(AgentBlueprint, ids["agent_id"])
        owner_current = CurrentAccount(owner, frozenset({"user", "builder"}))
        member_current = CurrentAccount(member, frozenset({"user"}))
        assert not await has_agent_use_access(
            db, agent=agent, account_id=member.id, roles=member_current.roles,
        )

        granted = await replace_agent_members(
            agent_id=agent.id,
            body=AgentMemberReplaceRequest(account_ids=[member.id]),
            if_match='W/"1"', idempotency_key="grant-member",
            db=db, current=owner_current,
        )
        assert granted.agent_version == 2
        assert [item.account_id for item in granted.members] == [member.id]

    async with factory() as db:
        member = await db.get(PlatformAccount, ids["member_id"])
        agent = await db.get(AgentBlueprint, ids["agent_id"])
        member_current = CurrentAccount(member, frozenset({"user"}))
        assert await has_agent_use_access(
            db, agent=agent, account_id=member.id, roles=member_current.roles,
        )
        listed = list(await db.scalars(
            select(AgentBlueprint).where(agent_use_filter(member.id))
        ))
        assert [item.id for item in listed] == [agent.id]
        assert (await _get_accessible_agent(db, agent.id, member_current)).id == agent.id
        with pytest.raises(HTTPException) as denied:
            await _get_owned_agent(db, agent.id, member_current)
        assert denied.value.status_code == 403

    async with factory() as db:
        owner = await db.get(PlatformAccount, ids["owner_id"])
        owner_current = CurrentAccount(owner, frozenset({"user", "builder"}))
        revoked = await replace_agent_members(
            agent_id=ids["agent_id"],
            body=AgentMemberReplaceRequest(account_ids=[]),
            if_match='W/"2"', idempotency_key="revoke-member",
            db=db, current=owner_current,
        )
        assert revoked.agent_version == 3
        assert revoked.members == []

    async with factory() as db:
        agent = await db.get(AgentBlueprint, ids["agent_id"])
        grant = await db.scalar(select(AgentMember).where(
            AgentMember.agent_id == agent.id,
            AgentMember.account_id == ids["member_id"],
        ))
        audits = list(await db.scalars(select(PlatformAuditEvent).where(
            PlatformAuditEvent.action == "agent.members_replaced",
        )))
        assert grant.status == "revoked"
        assert grant.revoked_at is not None
        assert len(audits) == 2
        assert not await has_agent_use_access(
            db, agent=agent, account_id=ids["member_id"], roles={"user"},
        )
        member = await db.get(PlatformAccount, ids["member_id"])
        actor = SessionActor(account=member, roles=frozenset({"user"}))
        # Revocation does not erase the user's historical Session shell.
        assert (await _owned_session(db, ids["session_id"], actor)).id == ids["session_id"]
        # It does block every new Run, including messages sent into that Session.
        with pytest.raises(HTTPException) as revoked_message:
            await create_message(
                session_id=ids["session_id"],
                body=MessageCreateRequest(content="continue"),
                idempotency_key="after-revocation", db=db, actor=actor,
            )
        assert revoked_message.value.status_code == 404


@pytest.mark.asyncio
async def test_whitelist_rejects_owner_and_cross_tenant_accounts(member_db):
    factory, ids = member_db
    async with factory() as db:
        owner = await db.get(PlatformAccount, ids["owner_id"])
        current = CurrentAccount(owner, frozenset({"user", "builder"}))
        with pytest.raises(HTTPException) as owner_error:
            await replace_agent_members(
                agent_id=ids["agent_id"],
                body=AgentMemberReplaceRequest(account_ids=[owner.id]),
                if_match='W/"1"', idempotency_key="owner-invalid",
                db=db, current=current,
            )
        assert owner_error.value.status_code == 422
        await db.rollback()

    async with factory() as db:
        owner = await db.get(PlatformAccount, ids["owner_id"])
        current = CurrentAccount(owner, frozenset({"user", "builder"}))
        with pytest.raises(HTTPException) as tenant_error:
            await replace_agent_members(
                agent_id=ids["agent_id"],
                body=AgentMemberReplaceRequest(account_ids=[ids["outsider_id"]]),
                if_match='W/"1"', idempotency_key="tenant-invalid",
                db=db, current=current,
            )
        assert tenant_error.value.status_code == 422
