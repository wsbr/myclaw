"""Directory projection for people an Agent may contact inside one Tenant."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select

from app.models.agent_v1 import AgentBlueprint, AgentMember
from app.models.foundation import PlatformAccount
from app.models.tenant_v1 import TenantMembership
from app.services.agent_access_service import has_agent_use_access


@dataclass(frozen=True)
class AgentPerson:
    account_id: str
    user_id: str
    display_name: str
    email: str | None

    @property
    def handle(self) -> str:
        # user_id is already immutable and globally unique in the current
        # identity model.  A dedicated Tenant-local handle can replace this
        # projection later without changing model-facing principal refs.
        return self.user_id


async def _agent(db, *, tenant_id: str, agent_id: str) -> AgentBlueprint | None:
    return await db.scalar(select(AgentBlueprint).where(
        AgentBlueprint.id == agent_id,
        AgentBlueprint.tenant_id == tenant_id,
    ))


def _eligible_statement(agent: AgentBlueprint):
    statement = select(PlatformAccount).join(TenantMembership, and_(
        TenantMembership.account_id == PlatformAccount.id,
        TenantMembership.tenant_id == PlatformAccount.tenant_id,
        TenantMembership.status == "active",
    )).where(
        PlatformAccount.tenant_id == agent.tenant_id,
        PlatformAccount.status == "active",
    )
    if agent.visibility == "private":
        statement = statement.outerjoin(AgentMember, and_(
            AgentMember.agent_id == agent.id,
            AgentMember.tenant_id == agent.tenant_id,
            AgentMember.account_id == PlatformAccount.id,
            AgentMember.status == "active",
        )).where(or_(
            PlatformAccount.id == agent.owner_account_id,
            AgentMember.id.is_not(None),
        ))
    return statement


async def list_agent_people(
    db, *, tenant_id: str, agent_id: str, requester_account_id: str,
    query: str = "", limit: int = 20, exclude_requester: bool = True,
) -> list[AgentPerson]:
    agent = await _agent(db, tenant_id=tenant_id, agent_id=agent_id)
    if agent is None or not await has_agent_use_access(
        db, agent=agent, account_id=requester_account_id,
    ):
        return []
    statement = _eligible_statement(agent)
    if exclude_requester:
        statement = statement.where(PlatformAccount.id != requester_account_id)
    normalized = query.strip().lower()
    if normalized:
        pattern = f"%{normalized}%"
        statement = statement.where(or_(
            func.lower(PlatformAccount.display_name).like(pattern),
            func.lower(PlatformAccount.user_id).like(pattern),
            func.lower(func.coalesce(PlatformAccount.email, "")).like(pattern),
        ))
    rows = list(await db.scalars(statement.order_by(
        PlatformAccount.display_name, PlatformAccount.user_id,
    ).limit(max(1, min(limit, 50)))))
    return [AgentPerson(
        account_id=item.id,
        user_id=item.user_id,
        display_name=item.display_name,
        email=item.email,
    ) for item in rows]


async def resolve_agent_person(
    db, *, tenant_id: str, agent_id: str, requester_account_id: str,
    target_account_id: str,
) -> AgentPerson | None:
    agent = await _agent(db, tenant_id=tenant_id, agent_id=agent_id)
    if agent is None or not await has_agent_use_access(
        db, agent=agent, account_id=requester_account_id,
    ):
        return None
    if target_account_id == requester_account_id:
        account = await db.scalar(select(PlatformAccount).join(
            TenantMembership, and_(
                TenantMembership.account_id == PlatformAccount.id,
                TenantMembership.tenant_id == PlatformAccount.tenant_id,
                TenantMembership.status == "active",
            ),
        ).where(
            PlatformAccount.id == target_account_id,
            PlatformAccount.tenant_id == tenant_id,
            PlatformAccount.status == "active",
        ))
    else:
        account = await db.scalar(_eligible_statement(agent).where(
            PlatformAccount.id == target_account_id,
        ))
    if account is None:
        return None
    return AgentPerson(
        account_id=account.id,
        user_id=account.user_id,
        display_name=account.display_name,
        email=account.email,
    )
