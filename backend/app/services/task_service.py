from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException
from app.models.task import Task
from app.models.agent import Agent
from app.models.user import User
from app.schemas.task import TaskCreate
import uuid


class TaskService:

    @staticmethod
    async def list(db: AsyncSession, current_user: User, agent_id: str | None = None, status: str | None = None) -> list[Task]:
        q = select(Task)
        if current_user.role != "admin":
            q = q.where(Task.user_id == current_user.id)
        if agent_id:
            q = q.where(Task.agent_id == agent_id)
        if status:
            q = q.where(Task.status == status)
        result = await db.execute(q.order_by(Task.created_at.desc()))
        return result.scalars().all()

    @staticmethod
    async def get_or_404(db: AsyncSession, task_id: str, current_user: User) -> Task:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if current_user.role != "admin" and task.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        return task

    @staticmethod
    async def create(db: AsyncSession, current_user: User, body: TaskCreate, session_id: str | None = None) -> Task:
        result = await db.execute(select(Agent).where(Agent.id == body.agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        task = Task(
            id=str(uuid.uuid4()),
            agent_id=body.agent_id,
            agent_version=agent.version,
            user_id=current_user.id,
            session_id=session_id,
            title=body.title,
            trigger_type=body.trigger_type,
            cron_expr=body.cron_expr,
            event_type=body.event_type,
            input_data=body.input,
            output_channel=body.output_channel,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

        if body.trigger_type == "immediate":
            from app.workers.task_worker import execute_task
            result = execute_task.delay(task.id)
            task.celery_task_id = result.id
            await db.commit()

        return task

    @staticmethod
    async def cancel(db: AsyncSession, task_id: str, current_user: User) -> None:
        task = await TaskService.get_or_404(db, task_id, current_user)
        if task.celery_task_id:
            from app.workers.celery_app import celery_app
            celery_app.control.revoke(task.celery_task_id, terminate=True)
        task.status = "cancelled"
        await db.commit()

    @staticmethod
    async def set_waiting(db: AsyncSession, task_id: str, reason: str) -> None:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task:
            task.status = "waiting"
            task.waiting_reason = reason
            await db.commit()

    @staticmethod
    async def set_running(db: AsyncSession, task_id: str) -> None:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task:
            task.status = "running"
            task.waiting_reason = None
            await db.commit()
