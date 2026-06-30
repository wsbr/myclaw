import gzip
import json
import uuid
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.session import Runner, Span


def _compress(data: dict) -> bytes:
    return gzip.compress(json.dumps(data, default=str).encode())


def _decompress(data: bytes) -> dict:
    return json.loads(gzip.decompress(data))


class Tracer:
    def __init__(self, db: AsyncSession, runner: Runner):
        self.db = db
        self.runner = runner

    async def start_span(
        self,
        span_type: str,
        name: str,
        snapshot: dict,
        parent_span_id: str | None = None,
    ) -> Span:
        span = Span(
            id=str(uuid.uuid4()),
            runner_id=self.runner.id,
            session_id=self.runner.session_id,   # 补写 session_id
            parent_span_id=parent_span_id,
            span_type=span_type,
            name=name,
            status="running",
            snapshot=_compress(snapshot),
            started_at=datetime.utcnow(),
        )
        self.db.add(span)
        await self.db.flush()

        # Real-time WebSocket broadcast
        await self._broadcast("span_started", {
            "span_id": span.id,
            "runner_id": span.runner_id,
            "span_type": span_type,
            "name": name,
        })
        return span

    async def end_span(
        self,
        span: Span,
        request: dict | None = None,
        response: dict | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        error: str | None = None,
    ) -> None:
        span.status = "failed" if error else "completed"
        span.completed_at = datetime.utcnow()
        span.duration_ms = int((span.completed_at - span.started_at).total_seconds() * 1000)
        span.input_tokens = input_tokens
        span.output_tokens = output_tokens
        span.cost_usd = cost_usd
        span.error = error
        if request:
            span.request = _compress(request)
        if response:
            span.response = _compress(response)
        await self.db.flush()

        # Accumulate on runner (use total_input/output_tokens if defined, fallback to total_tokens)
        if hasattr(self.runner, "total_input_tokens"):
            self.runner.total_input_tokens = (self.runner.total_input_tokens or 0) + input_tokens
            self.runner.total_output_tokens = (self.runner.total_output_tokens or 0) + output_tokens
        else:
            self.runner.total_tokens = (self.runner.total_tokens or 0) + input_tokens + output_tokens
        self.runner.total_cost_usd = (self.runner.total_cost_usd or 0) + cost_usd

        await self._broadcast("span_completed", {
            "span_id": span.id,
            "status": span.status,
            "duration_ms": span.duration_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        })

    async def end_runner(self, output: dict | None = None, error: str | None = None) -> None:
        self.runner.status = "failed" if error else "completed"
        self.runner.completed_at = datetime.utcnow()
        self.runner.duration_ms = int(
            (self.runner.completed_at - (self.runner.started_at or self.runner.created_at)).total_seconds() * 1000
        ) if self.runner.started_at else None
        self.runner.output_data = output
        self.runner.error = error
        await self.db.commit()

        await self._broadcast("runner_completed", {
            "runner_id": self.runner.id,
            "status": self.runner.status,
        })

    async def _broadcast(self, event_type: str, data: dict) -> None:
        """Push real-time event to WebSocket subscribers via Redis pub/sub."""
        try:
            import redis.asyncio as aioredis
            from app.config import settings
            payload = json.dumps({"type": event_type, "data": data})
            async with aioredis.from_url(settings.redis_url, decode_responses=True) as r:
                await r.publish(f"myclaw:session:{self.runner.session_id}", payload)
        except Exception:
            pass  # Broadcast failure must not interrupt execution
