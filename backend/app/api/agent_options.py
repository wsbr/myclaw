from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import CurrentAccount, get_current_builder
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.services.agent_access_service import agent_use_filter
from app.models.registry import (
    ToolDefinition, ToolGovernance, ToolVersion,
    PolicyDefinition, PolicyVersion,
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
    rows = list((await db.execute(
        select(ToolDefinition, ToolGovernance, ToolVersion)
        .join(ToolGovernance, ToolGovernance.tool_definition_id == ToolDefinition.id)
        .join(ToolVersion, ToolVersion.id == ToolDefinition.current_version_id)
        .where(ToolDefinition.status == "active", ToolGovernance.enabled.is_(True))
        .order_by(ToolDefinition.tool_key)
    )).all())
    return [{
        "id": definition.id, "key": definition.tool_key,
        "name": definition.display_name, "description": definition.summary,
        "execution_mode": version.execution_mode, "tool_version_id": version.id,
        "namespace": definition.namespace, "origin": definition.origin,
    } for definition, _, version in rows]


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
    rows = list(await db.scalars(select(PolicyDefinition).join(
        PolicyVersion, PolicyVersion.id == PolicyDefinition.current_version_id,
    ).where(
        PolicyDefinition.policy_type == policy_type, PolicyDefinition.enabled.is_(True),
        PolicyDefinition.status == "active", PolicyVersion.status == "published",
        PolicyVersion.policy_definition_id == PolicyDefinition.id,
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
        .where(
            AgentBlueprint.status == "published",
            agent_use_filter(current.account.id),
        )
        .order_by(AgentAssemblyVersion.name)
    ))
    return [
        {"id": agent.id, "name": version.name, "description": version.description,
         "visibility": agent.visibility, "group_enabled": version.group_enabled}
        for agent, version in rows
    ]
