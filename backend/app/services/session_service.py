from __future__ import annotations
import gzip
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
    async def get_messages(db: AsyncSession, session_id: str, current_user: User) -> list[dict]:
        """Reconstruct a chat-bubble message list from this session's completed Runners.

        Lets the frontend re-hydrate a conversation after the page was closed and
        reopened, instead of only offering the raw Span/Trace view.
        """
        await SessionService.get_or_404(db, session_id, current_user)
        result = await db.execute(
            select(Runner)
            .where(Runner.session_id == session_id, Runner.status == "completed")
            .order_by(Runner.created_at)
        )
        messages: list[dict] = []
        for r in result.scalars().all():
            if r.input_data:
                user_msg = r.input_data.get("message") or r.input_data.get("description", "")
                if user_msg:
                    messages.append({"role": "user", "content": user_msg, "created_at": r.created_at.isoformat()})
            if r.output_data:
                assistant_msg = r.output_data.get("content") or r.output_data.get("summary", "")
                if assistant_msg:
                    messages.append({"role": "assistant", "content": assistant_msg, "created_at": r.created_at.isoformat()})
        return messages

    @staticmethod
    def _decode_blob(data: bytes | None) -> dict | None:
        if data is None:
            return None
        try:
            return json.loads(gzip.decompress(data))
        except Exception:
            return None

    @staticmethod
    def _serialize_span(span: Span) -> dict:
        return {
            "id": span.id,
            "runner_id": span.runner_id,
            "session_id": span.session_id,
            "parent_span_id": span.parent_span_id,
            "span_type": span.span_type,
            "name": span.name,
            "status": span.status,
            "snapshot": SessionService._decode_blob(span.snapshot),
            "request": SessionService._decode_blob(span.request),
            "response": SessionService._decode_blob(span.response),
            "input_tokens": span.input_tokens,
            "output_tokens": span.output_tokens,
            "cost_usd": span.cost_usd,
            "duration_ms": span.duration_ms,
            "error": span.error,
            "started_at": span.started_at.isoformat() if span.started_at else None,
            "completed_at": span.completed_at.isoformat() if span.completed_at else None,
        }

    @staticmethod
    async def list_spans(db: AsyncSession, runner_id: str, current_user: User) -> list[dict]:
        result = await db.execute(
            select(Span).where(Span.runner_id == runner_id).order_by(Span.started_at)
        )
        return [SessionService._serialize_span(s) for s in result.scalars().all()]
