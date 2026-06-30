from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import engine
from app.models import *  # noqa: F401, F403 — ensures all models registered
from app.api.router import api_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _init_admin()
    yield


async def _init_admin():
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.user import User
    from app.core.security import hash_password
    import uuid

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == settings.init_admin_email))
        if result.scalar_one_or_none() is None:
            admin = User(
                id=str(uuid.uuid4()),
                email=settings.init_admin_email,
                username="admin",
                password_hash=hash_password(settings.init_admin_password),
                role="admin",
            )
            db.add(admin)
            await db.commit()


app = FastAPI(
    title="MyClaw Agent Platform",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


# WebSocket manager for real-time Span events
class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, session_id: str, ws: WebSocket):
        await ws.accept()
        self._connections.setdefault(session_id, []).append(ws)

    def disconnect(self, session_id: str, ws: WebSocket):
        if session_id in self._connections:
            try:
                self._connections[session_id].remove(ws)
            except ValueError:
                pass

    async def broadcast(self, session_id: str, message: dict):
        for ws in self._connections.get(session_id, []):
            try:
                await ws.send_json(message)
            except Exception:
                pass


manager = ConnectionManager()


@app.websocket("/ws/sessions/{session_id}")
async def websocket_endpoint(session_id: str, ws: WebSocket):
    import asyncio
    import json
    import redis.asyncio as aioredis

    await manager.connect(session_id, ws)
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe(f"myclaw:session:{session_id}")

    async def forward_redis():
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                try:
                    await ws.send_text(msg["data"])
                except Exception:
                    break

    forward_task = asyncio.create_task(forward_redis())
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        forward_task.cancel()
        await pubsub.unsubscribe(f"myclaw:session:{session_id}")
        await r.aclose()
        manager.disconnect(session_id, ws)


@app.get("/health")
async def health():
    return {"status": "ok"}
