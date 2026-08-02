"""Redis-backed fixed-window request limiting without storing raw principals."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

from fastapi import Request


_CONSUME_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    reset_epoch: int


def request_identifier(request: Request) -> str:
    authorization = request.headers.get("authorization", "").strip()
    if authorization:
        return f"authorization:{authorization}"
    client_host = request.client.host if request.client else "unknown"
    return f"network:{client_host}"


def _key(identifier: str, secret: str, window_seconds: int, now: int) -> str:
    digest = hmac.new(secret.encode(), identifier.encode(), hashlib.sha256).hexdigest()
    return f"myclaw:rate:{window_seconds}:{now // window_seconds}:{digest}"


async def consume(
    redis_client, *, identifier: str, secret: str, limit: int,
    window_seconds: int, now: int | None = None,
) -> RateLimitDecision:
    now = int(time.time()) if now is None else now
    count, ttl = await redis_client.eval(
        _CONSUME_SCRIPT, 1, _key(identifier, secret, window_seconds, now), window_seconds,
    )
    count, ttl = int(count), max(1, int(ttl))
    return RateLimitDecision(
        allowed=count <= limit,
        limit=limit,
        remaining=max(0, limit - count),
        retry_after=ttl if count > limit else 0,
        reset_epoch=now + ttl,
    )
