from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.services.task_service import TaskService
from app.schemas.task import TaskCreate, TaskResponse

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    agent_id: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await TaskService.list(db, current_user, agent_id=agent_id, status=status)


@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(
    body: TaskCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await TaskService.create(db, current_user, body)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await TaskService.get_or_404(db, task_id, current_user)


@router.delete("/{task_id}", status_code=204)
async def cancel_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await TaskService.cancel(db, task_id, current_user)


@router.get("/{task_id}/runners")
async def get_task_runners(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import select
    from app.models.session import Runner
    from app.models.task import Task
    task = await TaskService.get_or_404(db, task_id, current_user)
    if not task.session_id:
        return []
    result = await db.execute(
        select(Runner)
        .where(Runner.session_id == task.session_id)
        .order_by(Runner.created_at)
    )
    return result.scalars().all()
