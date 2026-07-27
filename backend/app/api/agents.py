from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.deps import get_current_user, require_owner_or_admin
from app.models.user import User
from app.models.agent import Agent
from app.schemas.agent import AgentCreate, AgentUpdate, AgentResponse, AgentListResponse
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[AgentListResponse])
async def list_agents(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await AgentService.list(db, current_user)


@router.post("", response_model=AgentResponse, status_code=201)
async def create_agent(
    body: AgentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await AgentService.create(db, current_user, body)


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await AgentService.get_or_404(db, agent_id)
    require_owner_or_admin(agent.owner_id, current_user)
    return await AgentService.to_response(db, agent)


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await AgentService.get_or_404(db, agent_id)
    require_owner_or_admin(agent.owner_id, current_user)
    return await AgentService.update(db, agent, body)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await AgentService.get_or_404(db, agent_id)
    require_owner_or_admin(agent.owner_id, current_user)
    await AgentService.delete(db, agent)


@router.post("/{agent_id}/activate", response_model=AgentResponse)
async def activate_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await AgentService.get_or_404(db, agent_id)
    require_owner_or_admin(agent.owner_id, current_user)
    return await AgentService.activate(db, agent)


@router.put("/{agent_id}/tools")
async def update_agent_tools(
    agent_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await AgentService.get_or_404(db, agent_id)
    require_owner_or_admin(agent.owner_id, current_user)
    await AgentService.update_tools(db, agent, body)
    return {"ok": True}


@router.put("/{agent_id}/skills")
async def update_agent_skills(
    agent_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await AgentService.get_or_404(db, agent_id)
    require_owner_or_admin(agent.owner_id, current_user)
    await AgentService.update_skills(db, agent, body)
    return {"ok": True}


@router.put("/{agent_id}/mcp-servers")
async def update_agent_mcp_servers(
    agent_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await AgentService.get_or_404(db, agent_id)
    require_owner_or_admin(agent.owner_id, current_user)
    await AgentService.update_mcp_servers(db, agent, body)
    return {"ok": True}


@router.get("/{agent_id}/versions")
async def get_agent_versions(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await AgentService.get_or_404(db, agent_id)
    require_owner_or_admin(agent.owner_id, current_user)
    return await AgentService.list_versions(db, agent_id)


@router.post("/{agent_id}/duplicate", response_model=AgentResponse, status_code=201)
async def duplicate_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await AgentService.get_or_404(db, agent_id)
    require_owner_or_admin(agent.owner_id, current_user)
    return await AgentService.duplicate(db, agent, current_user)


@router.get("/{agent_id}/api-tokens")
async def list_api_tokens(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await AgentService.get_or_404(db, agent_id)
    require_owner_or_admin(agent.owner_id, current_user)
    return await AgentService.list_api_tokens(db, agent_id)


@router.post("/{agent_id}/api-tokens", status_code=201)
async def create_api_token(
    agent_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await AgentService.get_or_404(db, agent_id)
    require_owner_or_admin(agent.owner_id, current_user)
    return await AgentService.create_api_token(db, agent_id, current_user.id, body.get("name", "token"))


@router.delete("/{agent_id}/api-tokens/{token_id}", status_code=204)
async def delete_api_token(
    agent_id: str,
    token_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await AgentService.get_or_404(db, agent_id)
    require_owner_or_admin(agent.owner_id, current_user)
    await AgentService.delete_api_token(db, token_id)
