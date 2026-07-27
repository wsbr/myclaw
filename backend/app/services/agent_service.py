from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from fastapi import HTTPException, status
from app.models.agent import Agent, AgentVersion, AgentTool, AgentSkill, AgentMcpServer, AgentBuiltinTool
from app.models.user import User, ApiToken
from app.schemas.agent import AgentCreate, AgentUpdate, AgentResponse
import uuid
import secrets
import hashlib


class AgentService:

    @staticmethod
    async def list(db: AsyncSession, current_user: User) -> list[Agent]:
        q = select(Agent)
        if current_user.role != "admin":
            q = q.where(Agent.owner_id == current_user.id)
        result = await db.execute(q.order_by(Agent.created_at.desc()))
        return result.scalars().all()

    @staticmethod
    async def get_or_404(db: AsyncSession, agent_id: str) -> Agent:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
        return agent

    @staticmethod
    async def to_response(db: AsyncSession, agent: Agent) -> AgentResponse:
        result = await db.execute(
            select(Agent)
            .where(Agent.id == agent.id)
            .options(
                selectinload(Agent.agent_tools),
                selectinload(Agent.agent_skills),
                selectinload(Agent.agent_mcp_servers),
                selectinload(Agent.agent_builtin_tools),
            )
        )
        loaded = result.scalar_one()
        base = AgentResponse.model_validate(loaded)
        return base.model_copy(update={
            "tool_ids": [
                t.tool_id for t in sorted(loaded.agent_tools, key=lambda row: row.sort_order)
            ],
            "skill_ids": [
                s.skill_id for s in sorted(loaded.agent_skills, key=lambda row: row.sort_order)
            ],
            "mcp_server_ids": [
                m.mcp_server_id for m in sorted(loaded.agent_mcp_servers, key=lambda row: row.sort_order)
            ],
            "builtin_tools": [
                t.tool_key for t in loaded.agent_builtin_tools if t.enabled
            ],
        })

    @staticmethod
    async def create(db: AsyncSession, owner: User, body: AgentCreate) -> Agent:
        agent = Agent(
            id=str(uuid.uuid4()),
            owner_id=owner.id,
            name=body.name,
            description=body.description,
            avatar_url=body.avatar_url,
            modes=body.modes,
            model_config_=body.model_config_,
            system_prompt_blocks=body.system_prompt_blocks,
            context_strategy=body.context_strategy,
            memory_config=body.memory_config,
            sandbox_config=body.sandbox_config,
            supported_channels=body.supported_channels,
            group_config=body.group_config,
            alert_config=body.alert_config,
        )
        db.add(agent)
        await db.flush()
        await AgentService._save_version(db, agent)
        await db.commit()
        await db.refresh(agent)
        return agent

    @staticmethod
    async def update(db: AsyncSession, agent: Agent, body: AgentUpdate) -> Agent:
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(agent, field, value)
        agent.version += 1
        await db.flush()
        await AgentService._save_version(db, agent)
        await db.commit()
        await db.refresh(agent)
        return agent

    @staticmethod
    async def delete(db: AsyncSession, agent: Agent) -> None:
        await db.delete(agent)
        await db.commit()

    @staticmethod
    async def activate(db: AsyncSession, agent: Agent) -> Agent:
        agent.status = "active"
        await db.commit()
        await db.refresh(agent)
        return agent

    @staticmethod
    async def _save_version(db: AsyncSession, agent: Agent) -> None:
        snapshot = {
            "id": agent.id, "name": agent.name, "description": agent.description,
            "modes": agent.modes, "model_config": agent.model_config_,
            "system_prompt_blocks": agent.system_prompt_blocks,
            "context_strategy": agent.context_strategy,
            "memory_config": agent.memory_config, "sandbox_config": agent.sandbox_config,
            "supported_channels": agent.supported_channels,
            "group_config": agent.group_config,
        }
        version = AgentVersion(
            id=str(uuid.uuid4()),
            agent_id=agent.id,
            version=agent.version,
            snapshot=snapshot,
        )
        db.add(version)

    @staticmethod
    async def update_tools(db: AsyncSession, agent: Agent, body: dict) -> None:
        await db.execute(
            AgentTool.__table__.delete().where(AgentTool.agent_id == agent.id)
        )
        await db.execute(
            AgentBuiltinTool.__table__.delete().where(AgentBuiltinTool.agent_id == agent.id)
        )
        for i, tool_id in enumerate(body.get("tool_ids", [])):
            db.add(AgentTool(agent_id=agent.id, tool_id=tool_id, sort_order=i))
        for key, enabled in body.get("builtin_tools", {}).items():
            if enabled:
                db.add(AgentBuiltinTool(agent_id=agent.id, tool_key=key, enabled=True))
        await db.commit()

    @staticmethod
    async def update_skills(db: AsyncSession, agent: Agent, body: dict) -> None:
        await db.execute(
            AgentSkill.__table__.delete().where(AgentSkill.agent_id == agent.id)
        )
        for i, skill_id in enumerate(body.get("skill_ids", [])):
            db.add(AgentSkill(agent_id=agent.id, skill_id=skill_id, sort_order=i))
        await db.commit()

    @staticmethod
    async def update_mcp_servers(db: AsyncSession, agent: Agent, body: dict) -> None:
        await db.execute(
            AgentMcpServer.__table__.delete().where(AgentMcpServer.agent_id == agent.id)
        )
        for i, server_id in enumerate(body.get("mcp_server_ids", [])):
            db.add(AgentMcpServer(agent_id=agent.id, mcp_server_id=server_id, sort_order=i))
        await db.commit()

    @staticmethod
    async def get_snapshot(db: AsyncSession, agent_id: str, version: int | None = None) -> dict:
        if version is None:
            agent = await AgentService.get_or_404(db, agent_id)
            version = agent.version
        result = await db.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent_id,
                AgentVersion.version == version,
            )
        )
        av = result.scalar_one_or_none()
        if not av:
            raise HTTPException(status_code=404, detail="Agent version not found")
        return av.snapshot

    @staticmethod
    async def list_versions(db: AsyncSession, agent_id: str) -> list[dict]:
        result = await db.execute(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version.desc())
        )
        return [
            {"id": av.id, "version": av.version, "created_at": str(av.created_at)}
            for av in result.scalars().all()
        ]

    @staticmethod
    async def duplicate(db: AsyncSession, source: Agent, owner: User) -> Agent:
        new_agent = Agent(
            id=str(uuid.uuid4()),
            owner_id=owner.id,
            name=f"{source.name} (副本)",
            description=source.description,
            modes=source.modes,
            model_config_=source.model_config_,
            system_prompt_blocks=source.system_prompt_blocks,
            context_strategy=source.context_strategy,
            memory_config=source.memory_config,
            sandbox_config=source.sandbox_config,
            supported_channels=source.supported_channels,
            group_config=source.group_config,
            alert_config=source.alert_config,
            status="draft",
        )
        db.add(new_agent)
        await db.flush()
        await AgentService._save_version(db, new_agent)
        await db.commit()
        await db.refresh(new_agent)
        return new_agent

    @staticmethod
    async def list_api_tokens(db: AsyncSession, agent_id: str) -> list[dict]:
        result = await db.execute(
            select(ApiToken).where(ApiToken.agent_id == agent_id)
        )
        return [
            {"id": t.id, "name": t.name, "last_used_at": str(t.last_used_at), "expires_at": str(t.expires_at)}
            for t in result.scalars().all()
        ]

    @staticmethod
    async def create_api_token(db: AsyncSession, agent_id: str, user_id: str, name: str) -> dict:
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        token = ApiToken(
            id=str(uuid.uuid4()),
            user_id=user_id,
            agent_id=agent_id,
            name=name,
            token_hash=token_hash,
        )
        db.add(token)
        await db.commit()
        return {"id": token.id, "name": name, "token": raw_token}  # 只在创建时返回明文

    @staticmethod
    async def delete_api_token(db: AsyncSession, token_id: str) -> None:
        result = await db.execute(select(ApiToken).where(ApiToken.id == token_id))
        token = result.scalar_one_or_none()
        if token:
            await db.delete(token)
            await db.commit()

    @staticmethod
    async def get_group_agents(db: AsyncSession, exclude_id: str | None = None) -> list[dict]:
        q = select(Agent).where(Agent.status == "active")
        result = await db.execute(q)
        agents = result.scalars().all()
        return [
            {"id": a.id, "name": a.name, "description": a.description}
            for a in agents
            if a.group_config and a.group_config.get("enabled")
            and (exclude_id is None or a.id != exclude_id)
        ]
