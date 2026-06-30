from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException
from app.models.tool import Tool
from app.models.user import User
from app.schemas.tool import ToolCreate, ToolUpdate
import uuid


class ToolService:

    @staticmethod
    async def list(db: AsyncSession) -> list[Tool]:
        result = await db.execute(select(Tool).where(Tool.is_active == True).order_by(Tool.name))
        return result.scalars().all()

    @staticmethod
    async def create(db: AsyncSession, creator: User, body: ToolCreate) -> Tool:
        tool = Tool(
            id=str(uuid.uuid4()),
            name=body.name,
            display_name=body.display_name,
            description=body.description,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            created_by=creator.id,
        )
        db.add(tool)
        await db.commit()
        await db.refresh(tool)
        return tool

    @staticmethod
    async def update(db: AsyncSession, tool_id: str, body: ToolUpdate) -> Tool:
        result = await db.execute(select(Tool).where(Tool.id == tool_id))
        tool = result.scalar_one_or_none()
        if not tool:
            raise HTTPException(status_code=404, detail="Tool not found")
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(tool, field, value)
        await db.commit()
        await db.refresh(tool)
        return tool

    @staticmethod
    async def delete(db: AsyncSession, tool_id: str) -> None:
        result = await db.execute(select(Tool).where(Tool.id == tool_id))
        tool = result.scalar_one_or_none()
        if not tool:
            raise HTTPException(status_code=404, detail="Tool not found")
        tool.is_active = False
        await db.commit()
