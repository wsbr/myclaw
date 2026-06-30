import json
import uuid
from datetime import datetime
import redis.asyncio as aioredis
from app.config import settings

CHANNEL_PREFIX = "myclaw:event:"


def _redis():
    return aioredis.from_url(settings.redis_url, decode_responses=True)


class EventBus:

    @staticmethod
    async def publish(event_type: str, payload: dict, source_agent_id: str, source_session_id: str, db=None):
        event = {
            "id": str(uuid.uuid4()),
            "event_type": event_type,
            "payload": payload,
            "source_agent_id": source_agent_id,
            "source_session_id": source_session_id,
            "published_at": datetime.utcnow().isoformat(),
        }
        async with _redis() as r:
            await r.publish(f"{CHANNEL_PREFIX}{event_type}", json.dumps(event))
            # Also store in a list for late subscribers (TTL 24h)
            await r.lpush(f"{CHANNEL_PREFIX}history:{event_type}", json.dumps(event))
            await r.expire(f"{CHANNEL_PREFIX}history:{event_type}", 86400)

        # Trigger any agents subscribed to this event type
        if db:
            await EventBus._trigger_subscribed_agents(event_type, event, db)

    @staticmethod
    async def _trigger_subscribed_agents(event_type: str, event: dict, db):
        from app.models.agent import Agent
        from sqlalchemy import select

        result = await db.execute(select(Agent).where(Agent.status == "active"))
        agents = result.scalars().all()

        for agent in agents:
            gc = agent.group_config or {}
            if not gc.get("enabled"):
                continue
            for em in gc.get("event_manifest", []):
                if em.get("event_type") == event_type:
                    from app.models.task import Task
                    import uuid as _uuid
                    task = Task(
                        id=str(_uuid.uuid4()),
                        agent_id=agent.id,
                        agent_version=agent.version,
                        user_id=event.get("source_agent_id", "system"),
                        session_id=event.get("source_session_id"),
                        trigger_type="event",
                        event_type=event_type,
                        title=f"[Event] {event_type}",
                        input_data=event["payload"],
                        output_channel="platform_ui",
                    )
                    db.add(task)
                    await db.flush()
                    from app.workers.task_worker import execute_task
                    execute_task.delay(task.id)
        await db.commit()

    @staticmethod
    async def get_recent(event_type: str, limit: int = 20) -> list[dict]:
        async with _redis() as r:
            items = await r.lrange(f"{CHANNEL_PREFIX}history:{event_type}", 0, limit - 1)
        return [json.loads(i) for i in items]
