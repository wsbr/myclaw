from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException
from app.models.tool import Skill
from app.models.user import User
from app.schemas.skill import SkillCreate, SkillUpdate
import uuid


class SkillService:

    @staticmethod
    async def list(db: AsyncSession) -> list[Skill]:
        result = await db.execute(select(Skill).where(Skill.is_active == True).order_by(Skill.name))
        return result.scalars().all()

    @staticmethod
    async def create(db: AsyncSession, creator: User, body: SkillCreate) -> Skill:
        skill = Skill(
            id=str(uuid.uuid4()),
            name=body.name,
            display_name=body.display_name,
            description=body.description,
            content=body.content,
            created_by=creator.id,
        )
        db.add(skill)
        await db.commit()
        await db.refresh(skill)
        return skill

    @staticmethod
    async def update(db: AsyncSession, skill_id: str, current_user: User, body: SkillUpdate) -> Skill:
        result = await db.execute(select(Skill).where(Skill.id == skill_id))
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(status_code=404, detail="Skill not found")
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(skill, field, value)
        await db.commit()
        await db.refresh(skill)
        return skill

    @staticmethod
    async def delete(db: AsyncSession, skill_id: str, current_user: User) -> None:
        result = await db.execute(select(Skill).where(Skill.id == skill_id))
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(status_code=404, detail="Skill not found")
        skill.is_active = False
        await db.commit()
