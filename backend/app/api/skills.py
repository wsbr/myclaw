from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.services.skill_service import SkillService
from app.schemas.skill import SkillCreate, SkillUpdate, SkillResponse

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=list[SkillResponse])
async def list_skills(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    return await SkillService.list(db)


@router.post("", response_model=SkillResponse, status_code=201)
async def create_skill(
    body: SkillCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await SkillService.create(db, current_user, body)


@router.put("/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: str,
    body: SkillUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await SkillService.update(db, skill_id, current_user, body)


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(
    skill_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await SkillService.delete(db, skill_id, current_user)
