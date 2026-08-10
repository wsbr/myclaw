"""Best-effort ephemeral model streaming for the Session SSE channel."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from app.config import settings


logger = logging.getLogger(__name__)


@dataclass
class _StreamBuffer:
    sequence: int = 0
    published_first: bool = False
    pending: str = ""
    flush_task: asyncio.Task | None = None


class LiveStreamPublisher:
    """Publish provisional text without making Redis part of Run correctness."""

    def __init__(self, *, flush_seconds: float = 0.05, flush_chars: int = 128) -> None:
        self.flush_seconds = flush_seconds
        self.flush_chars = flush_chars
        self._redis = None
        self._buffers: dict[str, _StreamBuffer] = {}
        self._lock = asyncio.Lock()

    async def _publish(self, session_id: str, payload: dict) -> None:
        try:
            if self._redis is None:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(
                    settings.redis_url, decode_responses=True,
                )
            await asyncio.wait_for(
                self._redis.publish(
                    f"myclaw:session:{session_id}:live",
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
                timeout=0.25,
            )
        except Exception:
            logger.warning(
                "Ephemeral model stream publish failed",
                extra={"session_id": session_id, "event_type": payload.get("event_type")},
                exc_info=True,
            )
            client, self._redis = self._redis, None
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass

    @staticmethod
    def _base(
        *, session_id: str, run_id: str, runner_id: str,
        agent_turn_id: str, stream_id: str,
    ) -> dict:
        return {
            "session_id": session_id,
            "run_id": run_id,
            "runner_id": runner_id,
            "agent_turn_id": agent_turn_id,
            "stream_id": stream_id,
            "provisional": True,
        }

    async def publish_delta(
        self, *, session_id: str, run_id: str, runner_id: str,
        agent_turn_id: str, delta: str,
    ) -> None:
        if not delta:
            return
        stream_id = agent_turn_id
        immediate: tuple[int, str] | None = None
        async with self._lock:
            state = self._buffers.setdefault(stream_id, _StreamBuffer())
            if not state.published_first:
                state.published_first = True
                state.sequence += 1
                immediate = (state.sequence, delta)
            else:
                state.pending += delta
                if len(state.pending) >= self.flush_chars:
                    state.sequence += 1
                    immediate = (state.sequence, state.pending)
                    state.pending = ""
                    if state.flush_task is not None:
                        state.flush_task.cancel()
                        state.flush_task = None
                elif state.flush_task is None:
                    state.flush_task = asyncio.create_task(self._flush_after(
                        session_id=session_id, run_id=run_id, runner_id=runner_id,
                        agent_turn_id=agent_turn_id, stream_id=stream_id,
                    ))
        if immediate is not None:
            sequence, content = immediate
            await self._publish(session_id, {
                **self._base(
                    session_id=session_id, run_id=run_id, runner_id=runner_id,
                    agent_turn_id=agent_turn_id, stream_id=stream_id,
                ),
                "event_type": "model.delta", "sequence": sequence,
                "delta": content,
            })

    async def _flush_after(
        self, *, session_id: str, run_id: str, runner_id: str,
        agent_turn_id: str, stream_id: str,
    ) -> None:
        try:
            await asyncio.sleep(self.flush_seconds)
            await self.flush(
                session_id=session_id, run_id=run_id, runner_id=runner_id,
                agent_turn_id=agent_turn_id, stream_id=stream_id,
            )
        except asyncio.CancelledError:
            return

    async def flush(
        self, *, session_id: str, run_id: str, runner_id: str,
        agent_turn_id: str, stream_id: str | None = None,
    ) -> None:
        stream_id = stream_id or agent_turn_id
        outgoing: tuple[int, str] | None = None
        async with self._lock:
            state = self._buffers.get(stream_id)
            if state is not None:
                current = asyncio.current_task()
                if state.flush_task is not None and state.flush_task is not current:
                    state.flush_task.cancel()
                state.flush_task = None
                if state.pending:
                    state.sequence += 1
                    outgoing = (state.sequence, state.pending)
                    state.pending = ""
        if outgoing is not None:
            sequence, content = outgoing
            await self._publish(session_id, {
                **self._base(
                    session_id=session_id, run_id=run_id, runner_id=runner_id,
                    agent_turn_id=agent_turn_id, stream_id=stream_id,
                ),
                "event_type": "model.delta", "sequence": sequence,
                "delta": content,
            })

    async def finish(
        self, *, event_type: str, session_id: str, run_id: str,
        runner_id: str, agent_turn_id: str, reason_code: str | None = None,
        message_id: str | None = None,
    ) -> None:
        stream_id = agent_turn_id
        if event_type == "model.stream_committed":
            await self.flush(
                session_id=session_id, run_id=run_id, runner_id=runner_id,
                agent_turn_id=agent_turn_id, stream_id=stream_id,
            )
        async with self._lock:
            state = self._buffers.pop(stream_id, None)
            if state is not None and state.flush_task is not None:
                state.flush_task.cancel()
            sequence = (state.sequence if state is not None else 0) + 1
        payload = {
            **self._base(
                session_id=session_id, run_id=run_id, runner_id=runner_id,
                agent_turn_id=agent_turn_id, stream_id=stream_id,
            ),
            "event_type": event_type, "sequence": sequence,
        }
        if event_type == "model.stream_committed":
            payload["provisional"] = False
        if reason_code:
            payload["reason_code"] = reason_code
        if message_id:
            payload["message_id"] = message_id
        await self._publish(session_id, payload)

    async def publish_tool_progress(
        self, *, session_id: str, run_id: str, runner_id: str,
        call_id: str, tool_key: str, status: str,
        invocation_id: str | None = None,
    ) -> None:
        payload = {
            "event_type": "tool.progress",
            "session_id": session_id,
            "run_id": run_id,
            "runner_id": runner_id,
            "call_id": call_id,
            "tool_key": tool_key,
            "status": status,
        }
        if invocation_id:
            payload["tool_invocation_id"] = invocation_id
        await self._publish(session_id, payload)
