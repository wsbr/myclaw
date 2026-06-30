from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.deps import get_current_admin
from app.models.user import User
from app.models.agent import Agent
from app.models.task import Task
from app.schemas.auth import UserResponse
from app.core.security import hash_password
from pydantic import BaseModel
import uuid

router = APIRouter(prefix="/admin", tags=["admin"])


class UserCreate(BaseModel):
    email: str
    username: str
    password: str
    role: str = "user"


@router.get("/users", response_model=list[UserResponse])
async def list_users(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_admin)):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return [UserResponse.model_validate(u) for u in result.scalars().all()]


@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    user = User(
        id=str(uuid.uuid4()),
        email=body.email,
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


class UserUpdate(BaseModel):
    username: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


@router.put("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    body: UserUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found")
    if body.username is not None:
        user.username = body.username
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.password:
        user.password_hash = hash_password(body.password)
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        await db.delete(user)
        await db.commit()


@router.get("/agents")
async def list_all_agents(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_admin)):
    result = await db.execute(select(Agent).order_by(Agent.created_at.desc()))
    agents = result.scalars().all()
    return [
        {
            "id": a.id, "name": a.name, "status": a.status,
            "owner_id": a.owner_id, "created_at": a.created_at,
            "modes": a.modes,
        }
        for a in agents
    ]


@router.get("/stats")
async def global_stats(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_admin)):
    total_users = await db.scalar(select(func.count(User.id)))
    total_agents = await db.scalar(select(func.count(Agent.id)))
    total_tasks = await db.scalar(select(func.count(Task.id)))
    return {
        "total_users": total_users,
        "total_agents": total_agents,
        "total_tasks": total_tasks,
    }
