from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.core.rate_limit import consume, request_identifier
from app.main import request_identity


pytestmark = pytest.mark.unit


class FakeRedis:
    def __init__(self):
        self.counts: dict[str, int] = {}
        self.keys: list[str] = []

    async def eval(self, script, key_count, key, window_seconds):
        assert "INCR" in script and key_count == 1
        self.keys.append(key)
        self.counts[key] = self.counts.get(key, 0) + 1
        return [self.counts[key], int(window_seconds)]


def _request(*, authorization: str | None = None, host: str = "203.0.113.5") -> Request:
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    return Request({
        "type": "http", "method": "GET", "path": "/api/v1/agents",
        "headers": headers, "client": (host, 443), "scheme": "https",
        "server": ("test", 443), "query_string": b"",
    })


def test_identifier_prefers_authorization_and_never_trusts_forwarded_headers():
    assert request_identifier(_request(authorization="Bearer opaque")) == "authorization:Bearer opaque"
    assert request_identifier(_request()) == "network:203.0.113.5"


@pytest.mark.asyncio
async def test_fixed_window_limits_and_hashes_raw_principal():
    redis = FakeRedis()
    decisions = [
        await consume(
            redis, identifier="authorization:Bearer highly-sensitive-token",
            secret="rate-limit-test-secret", limit=2, window_seconds=60, now=120,
        )
        for _ in range(3)
    ]

    assert [item.allowed for item in decisions] == [True, True, False]
    assert [item.remaining for item in decisions] == [1, 0, 0]
    assert decisions[-1].retry_after == 60
    assert len(set(redis.keys)) == 1
    assert all("highly-sensitive-token" not in key for key in redis.keys)


@pytest.mark.asyncio
async def test_new_window_uses_a_distinct_counter():
    redis = FakeRedis()
    first = await consume(
        redis, identifier="network:203.0.113.5", secret="secret",
        limit=1, window_seconds=60, now=179,
    )
    second = await consume(
        redis, identifier="network:203.0.113.5", secret="secret",
        limit=1, window_seconds=60, now=180,
    )
    assert first.allowed and second.allowed
    assert redis.keys[0] != redis.keys[1]


@pytest.mark.asyncio
async def test_redis_failure_is_explicitly_degraded_without_blocking_authoritative_api(monkeypatch):
    async def unavailable(*args, **kwargs):
        raise ConnectionError("redis unavailable")

    async def downstream(request):
        return Response("ok", status_code=200)

    monkeypatch.setattr("app.main.consume", unavailable)
    request = _request()
    request.scope["app"] = type("App", (), {"state": type("State", (), {"rate_limit_redis": object()})()})()
    response = await request_identity(request, downstream)

    assert response.status_code == 200
    assert response.headers["X-RateLimit-State"] == "degraded"
    assert response.headers["X-Request-ID"]
