from __future__ import annotations
import json
from typing import AsyncIterator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException
from app.models.session import Session, Runner, Span
from app.models.user import User
from app.schemas.session import SessionCreate, ChatRequest, ForkRequest
import uuid


class SessionService:

    @staticmethod
    async def list(db: AsyncSession, current_user: User, agent_id: str | None = None) -> list[Session]:
        q = select(Session)
        if current_user.role != "admin":
            q = q.where(Session.user_id == current_user.id)
        if agent_id:
            q = q.where(Session.agent_id == agent_id)
        result = await db.execute(q.order_by(Session.created_at.desc()))
        return result.scalars().all()

    @staticmethod
    async def get_or_404(db: AsyncSession, session_id: str, current_user: User) -> Session:
        result = await db.execute(select(Session).where(Session.id == session_id))
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if current_user.role != "admin" and session.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        return session

    @staticmethod
    async def create(db: AsyncSession, current_user: User, body: SessionCreate) -> Session:
        session = Session(
            id=str(uuid.uuid4()),
            agent_id=body.agent_id,
            user_id=current_user.id,
            mode=body.mode,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session

    @staticmethod
    async def delete(db: AsyncSession, session_id: str, current_user: User) -> None:
        session = await SessionService.get_or_404(db, session_id, current_user)
        session.status = "archived"
        await db.commit()

    @staticmethod
    async def chat_stream(
        db: AsyncSession,
        session_id: str,
        current_user: User,
        body: ChatRequest,
    ) -> AsyncIterator[str]:
        session = await SessionService.get_or_404(db, session_id, current_user)

        from app.runtime.agent_runtime import AgentRuntime
        from app.services.agent_service import AgentService

        snapshot = await AgentService.get_snapshot(db, session.agent_id)

        runner = Runner(
            id=str(uuid.uuid4()),
            session_id=session_id,
            agent_id=session.agent_id,
            agent_version=snapshot.get("version", 1),
            user_id=current_user.id,
            input_data={"message": body.message},
        )
        db.add(runner)
        await db.commit()
        await db.refresh(runner)

        runtime = AgentRuntime(snapshot=snapshot, db=db, runner=runner, session=session)
        async for event in runtime.run(body.message):
            yield f"data: {json.dumps(event)}\n\n"

        yield "data: [DONE]\n\n"

    @staticmethod
    async def fork(db: AsyncSession, session_id: str, current_user: User, body: ForkRequest) -> Session:
        origin = await SessionService.get_or_404(db, session_id, current_user)
        fork_session = Session(
            id=str(uuid.uuid4()),
            agent_id=origin.agent_id,
            user_id=current_user.id,
            mode=origin.mode,
            fork_from_session_id=session_id,
            fork_from_runner_id=body.runner_id,
            fork_from_span_id=body.span_id,
            metadata_={"modifications": body.modifications},
        )
        db.add(fork_session)
        await db.commit()
        await db.refresh(fork_session)
        return fork_session

    @staticmethod
    async def list_runners(db: AsyncSession, session_id: str, current_user: User) -> list[Runner]:
        await SessionService.get_or_404(db, session_id, current_user)
        result = await db.execute(
            select(Runner).where(Runner.session_id == session_id).order_by(Runner.created_at)
        )
        return result.scalars().all()

    @staticmethod
    async def list_spans(db: AsyncSession, runner_id: str, current_user: User) -> list[Span]:
        result = await db.execute(
            select(Span).where(Span.runner_id == runner_id).order_by(Span.started_at)
        )
        return result.scalars().all()
