import json
import uuid
from datetime import datetime, timedelta
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from app.config import settings
from app.models.memory import Memory

SHORT_TERM_TTL = 3600  # 1 hour in-session


class MemoryManager:

    # ── Short-term (Redis, session-scoped) ─────────────────────────────────

    @staticmethod
    async def save_short_term(session_id: str, key: str, value: str) -> None:
        async with aioredis.from_url(settings.redis_url, decode_responses=True) as r:
            field = f"myclaw:memory:st:{session_id}"
            data = json.loads(await r.get(field) or "{}")
            data[key] = value
            await r.setex(field, SHORT_TERM_TTL, json.dumps(data))

    @staticmethod
    async def load_short_term(session_id: str) -> dict:
        async with aioredis.from_url(settings.redis_url, decode_responses=True) as r:
            data = await r.get(f"myclaw:memory:st:{session_id}")
            return json.loads(data) if data else {}

    # ── Long-term (MySQL via ORM) ──────────────────────────────────────────

    @staticmethod
    async def save_long_term(
        db: AsyncSession,
        agent_id: str,
        user_id: str,
        content: str,
        scope: str = "user",
        key: str | None = None,
        ttl_days: int | None = None,
        session_id: str | None = None,
    ) -> Memory:
        expires_at = datetime.utcnow() + timedelta(days=ttl_days) if ttl_days else None
        memory = Memory(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            memory_type="long_term",
            scope=scope,
            key=key or "",
            content=content,
            expires_at=expires_at,
        )
        db.add(memory)
        await db.commit()
        await db.refresh(memory)
        return memory

    @staticmethod
    async def load_long_term(
        db: AsyncSession,
        agent_id: str,
        user_id: str,
        scope: str = "user",
        limit: int = 20,
    ) -> list[dict]:
        now = datetime.utcnow()
        q = select(Memory).where(
            and_(
                Memory.agent_id == agent_id,
                Memory.user_id == user_id,
                Memory.scope == scope,
                Memory.memory_type == "long_term",
            )
        ).filter(
            (Memory.expires_at == None) | (Memory.expires_at > now)  # noqa: E711
        ).order_by(Memory.created_at.desc()).limit(limit)

        result = await db.execute(q)
        return [
            {
                "id": m.id,
                "key": m.key,
                "content": m.content,
                "scope": m.scope,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in result.scalars().all()
        ]

    @staticmethod
    async def delete_memory(db: AsyncSession, memory_id: str) -> None:
        result = await db.execute(select(Memory).where(Memory.id == memory_id))
        mem = result.scalar_one_or_none()
        if mem:
            await db.delete(mem)
            await db.commit()

    @staticmethod
    async def search_long_term(
        db: AsyncSession,
        agent_id: str,
        user_id: str,
        keyword: str,
        limit: int = 10,
    ) -> list[dict]:
        """Simple keyword search; vector search can replace this later."""
        from sqlalchemy import or_
        now = datetime.utcnow()
        q = select(Memory).where(
            and_(
                Memory.agent_id == agent_id,
                Memory.user_id == user_id,
                Memory.memory_type == "long_term",
                or_(
                    Memory.content.contains(keyword),
                    Memory.key.contains(keyword),
                ),
            )
        ).filter(
            (Memory.expires_at == None) | (Memory.expires_at > now)  # noqa: E711
        ).order_by(Memory.created_at.desc()).limit(limit)

        result = await db.execute(q)
        return [
            {"id": m.id, "key": m.key, "content": m.content}
            for m in result.scalars().all()
        ]
