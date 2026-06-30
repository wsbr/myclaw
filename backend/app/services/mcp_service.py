from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException
import httpx
from app.models.tool import McpServer
from app.models.user import User
from app.schemas.mcp_server import McpServerCreate, McpServerUpdate
import uuid


class McpService:

    @staticmethod
    async def list(db: AsyncSession) -> list[McpServer]:
        result = await db.execute(select(McpServer).order_by(McpServer.name))
        return result.scalars().all()

    @staticmethod
    async def create(db: AsyncSession, creator: User, body: McpServerCreate) -> McpServer:
        server = McpServer(
            id=str(uuid.uuid4()),
            name=body.name,
            display_name=body.display_name,
            description=body.description,
            endpoint=body.endpoint,
            auth_type=body.auth_type,
            health_check_url=body.health_check_url,
            capability_summary=body.capability_summary,
            created_by=creator.id,
        )
        db.add(server)
        await db.commit()
        await db.refresh(server)
        return server

    @staticmethod
    async def update(db: AsyncSession, server_id: str, current_user: User, body: McpServerUpdate) -> McpServer:
        result = await db.execute(select(McpServer).where(McpServer.id == server_id))
        server = result.scalar_one_or_none()
        if not server:
            raise HTTPException(status_code=404, detail="MCP Server not found")
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(server, field, value)
        await db.commit()
        await db.refresh(server)
        return server

    @staticmethod
    async def delete(db: AsyncSession, server_id: str, current_user: User) -> None:
        result = await db.execute(select(McpServer).where(McpServer.id == server_id))
        server = result.scalar_one_or_none()
        if not server:
            raise HTTPException(status_code=404, detail="MCP Server not found")
        server.is_active = False
        await db.commit()

    @staticmethod
    async def health_check(db: AsyncSession, server_id: str) -> dict:
        result = await db.execute(select(McpServer).where(McpServer.id == server_id))
        server = result.scalar_one_or_none()
        if not server:
            raise HTTPException(status_code=404, detail="MCP Server not found")
        url = server.health_check_url or server.endpoint
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
            return {"status": "healthy" if resp.status_code < 400 else "unhealthy", "latency_ms": 0}
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}
