from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import CurrentAccount, get_current_builder
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.registry import (
    PlatformTool,
    PolicyDefinition,
    RegisteredModel,
    RegisteredSkill,
    SandboxProfile,
)

router = APIRouter(prefix="/agent-options", tags=["agent-options"])


@router.get("/models")
async def models(db: AsyncSession = Depends(get_db), _: CurrentAccount = Depends(get_current_builder)):
    rows = list(await db.scalars(select(RegisteredModel).where(RegisteredModel.enabled.is_(True)).order_by(RegisteredModel.key)))
    return [{"id": row.id, "key": row.key, "name": row.model_name, "provider": row.provider, "capabilities": row.capabilities} for row in rows]


@router.get("/platform-tools")
async def tools(db: AsyncSession = Depends(get_db), _: CurrentAccount = Depends(get_current_builder)):
    rows = list(await db.scalars(select(PlatformTool).where(PlatformTool.enabled.is_(True)).order_by(PlatformTool.tool_key)))
    return [{"id": row.id, "key": row.tool_key, "name": row.display_name, "description": row.description, "execution_mode": row.execution_mode} for row in rows]


@router.get("/skills")
async def skills(db: AsyncSession = Depends(get_db), _: CurrentAccount = Depends(get_current_builder)):
    rows = list(await db.scalars(select(RegisteredSkill).where(
        RegisteredSkill.status == "active", RegisteredSkill.current_published_version_id.is_not(None)
    ).order_by(RegisteredSkill.logical_name)))
    return [{"id": row.id, "key": row.logical_name, "name": row.name, "description": row.description} for row in rows]


@router.get("/policy-definitions")
async def policies(
    policy_type: str = Query(), db: AsyncSession = Depends(get_db),
    _: CurrentAccount = Depends(get_current_builder),
):
    rows = list(await db.scalars(select(PolicyDefinition).where(
        PolicyDefinition.policy_type == policy_type, PolicyDefinition.enabled.is_(True)
    ).order_by(PolicyDefinition.policy_key)))
    return [{"id": row.id, "key": row.policy_key, "name": row.name, "description": row.description} for row in rows]


@router.get("/sandbox-profiles")
async def sandboxes(db: AsyncSession = Depends(get_db), _: CurrentAccount = Depends(get_current_builder)):
    rows = list(await db.scalars(select(SandboxProfile).where(SandboxProfile.enabled.is_(True)).order_by(SandboxProfile.profile_key)))
    return [{"id": row.id, "key": row.profile_key, "name": row.profile_key, "description": row.description} for row in rows]


@router.get("/agents")
async def bound_agents(db: AsyncSession = Depends(get_db), current: CurrentAccount = Depends(get_current_builder)):
    rows = list(await db.execute(
        select(AgentBlueprint, AgentAssemblyVersion)
        .join(AgentAssemblyVersion, AgentAssemblyVersion.id == AgentBlueprint.current_published_version_id)
        .where(AgentBlueprint.status == "published")
        .order_by(AgentAssemblyVersion.name)
    ))
    return [
        {"id": agent.id, "name": version.name, "description": version.description,
         "visibility": agent.visibility, "group_enabled": version.group_enabled}
        for agent, version in rows
        if agent.owner_account_id == current.account.id or agent.visibility == "enterprise"
    ]
