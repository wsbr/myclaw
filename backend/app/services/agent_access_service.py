"""One authorization boundary for discovering and using an Agent."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import and_, exists, or_, select

from app.models.agent_v1 import AgentBlueprint, AgentMember
from app.models.foundation import PlatformAccountRole


def active_agent_member_exists(account_id: str):
    """Return a correlated SQL predicate for an active private-Agent grant."""

    return exists(select(AgentMember.id).where(
        AgentMember.agent_id == AgentBlueprint.id,
        AgentMember.tenant_id == AgentBlueprint.tenant_id,
        AgentMember.account_id == account_id,
        AgentMember.status == "active",
    ))


def agent_use_filter(account_id: str):
    """Filter Agents visible to a non-admin account, including owned drafts."""

    return or_(
        AgentBlueprint.owner_account_id == account_id,
        and_(
            AgentBlueprint.status == "published",
            or_(
                AgentBlueprint.visibility == "enterprise",
                active_agent_member_exists(account_id),
            ),
        ),
    )


async def has_agent_use_access(
    db, *, agent: AgentBlueprint, account_id: str | None,
    roles: Collection[str] | None = None,
) -> bool:
    """Check discovery/execution access, distinct from management authority."""

    if account_id is None:
        return False
    if agent.owner_account_id == account_id:
        return True
    effective_roles = set(roles) if roles is not None else set(await db.scalars(
        select(PlatformAccountRole.role_key).where(
            PlatformAccountRole.account_id == account_id,
        )
    ))
    if "admin" in effective_roles:
        return True
    if agent.status != "published" or not agent.current_published_version_id:
        return False
    if agent.visibility == "enterprise":
        return True
    return bool(await db.scalar(select(AgentMember.id).where(
        AgentMember.agent_id == agent.id,
        AgentMember.tenant_id == agent.tenant_id,
        AgentMember.account_id == account_id,
        AgentMember.status == "active",
    ).limit(1)))
