from __future__ import annotations
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from app.models.notification import Notification


class NotificationService:

    @staticmethod
    async def create(
        db: AsyncSession,
        user_id: str,
        content: str,
        agent_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        channel: str = "platform_ui",
        title: str | None = None,
    ) -> Notification:
        notification = Notification(
            id=str(uuid.uuid4()),
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            task_id=task_id,
            channel=channel,
            title=title,
            content=content,
        )
        db.add(notification)
        await db.commit()
        await db.refresh(notification)
        return notification

    @staticmethod
    async def list_for_user(
        db: AsyncSession, user_id: str, unread_only: bool = False, limit: int = 50
    ) -> list[Notification]:
        q = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            q = q.where(Notification.is_read == False)  # noqa: E712
        q = q.order_by(Notification.created_at.desc()).limit(limit)
        result = await db.execute(q)
        return result.scalars().all()

    @staticmethod
    async def unread_count(db: AsyncSession, user_id: str) -> int:
        result = await db.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.is_read == False,  # noqa: E712
            )
        )
        return result.scalar_one()

    @staticmethod
    async def mark_read(db: AsyncSession, notification_id: str, user_id: str) -> None:
        await db.execute(
            update(Notification)
            .where(Notification.id == notification_id, Notification.user_id == user_id)
            .values(is_read=True)
        )
        await db.commit()

    @staticmethod
    async def mark_all_read(db: AsyncSession, user_id: str) -> None:
        await db.execute(
            update(Notification)
            .where(Notification.user_id == user_id, Notification.is_read == False)  # noqa: E712
            .values(is_read=True)
        )
        await db.commit()
