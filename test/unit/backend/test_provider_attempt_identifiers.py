"""Real SDK streaming over in-memory HTTP; no external provider requests."""
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.model_hub.anthropic import AnthropicLLM
from app.core.model_hub.base import LLMChunk
from app.core.model_hub.openai import OpenAILLM
from app.database import Base
from app.models.runtime_v1 import AgentTurn, ProviderAttempt
from app.runtime.model_adapter import ModelAdapter
from app.runtime.protocol import ProviderError, ProviderRequest
from app.runtime.provider_attempt_recorder import TurnRecorder

pytestmark = pytest.mark.unit
REQUEST = ProviderRequest(messages=({"role": "user", "content": "test"},), tools=(), parameters={})


@pytest_asyncio.fixture
async def recorder_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[ProviderAttempt.__table__]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            yield db
    finally:
        await engine.dispose()


def observed_attempts(db):
    return TurnRecorder(db, AgentTurn(id="turn"), SimpleNamespace(model_version_id="model", provider="test"), usage_ledger_enabled=False)


def sse(provider, stage, response_id="body-id", *, finish=False):
    def event(data):
        prefix = f"event: {data['type']}\n" if provider == "anthropic" else ""
        return (prefix + "data: " + json.dumps(data) + "\n\n").encode()
    if stage == "headers":
        return []
    if provider == "openai":
        delta = {"role": "assistant"}
        if stage == "text":
            delta["content"] = "partial text"
        elif stage == "tool":
            delta["tool_calls"] = [{"index": 0, "id": "call-1", "type": "function", "function": {"name": "example", "arguments": '{"partial":'}}]
        def chunk(delta, finish_reason=None):
            return event({"id": response_id, "object": "chat.completion.chunk", "created": 1, "model": "test", "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]})
        result = [chunk(delta)]
        if finish:
            result += [chunk({}, "stop"), b"data: [DONE]\n\n"]
        return result
    result = [event({"type": "message_start", "message": {"id": response_id, "type": "message", "role": "assistant", "model": "test", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}})]
    if stage == "text":
        result += [event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}), event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial text"}})]
    elif stage == "tool":
        result += [event({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call-1", "name": "example", "input": {}}}), event({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"partial":'}})]
    if finish:
        if stage in {"text", "tool"}:
            result.append(event({"type": "content_block_stop", "index": 0}))
        result += [event({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}}), event({"type": "message_stop"})]
    return result


class WireStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, fail=False, before_body=None):
        self.chunks, self.fail, self.before_body = chunks, fail, before_body
        self.closed = False

    async def __aiter__(self):
        if self.before_body:
            await self.before_body()
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body (incomplete chunked read)")

    async def aclose(self):
        self.closed = True


async def provider_with_transport(kind, handler):
    provider = (OpenAILLM if kind == "openai" else AnthropicLLM)("unused-local-fixture", base_url="https://fixture.invalid/v1")
    assert provider.client.max_retries == 0
    await provider.client._client.aclose()
    provider.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_header_request_id_is_durable_before_first_byte_and_survives_early_eof(recorder_db, kind):
    observed = observed_attempts(recorder_db)
    calls = 0
    async def before_body():
        attempt = await recorder_db.scalar(select(ProviderAttempt))
        assert attempt.provider_request_id == "header-id"
        assert attempt.status == "running" and attempt.ended_at is None
        assert not attempt.received_payload and attempt.first_payload_at is None
        await recorder_db.commit()
    wire = WireStream([], fail=True, before_body=before_body)
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "text/event-stream", "x-request-id": "header-id"}, stream=wire)
    provider = await provider_with_transport(kind, handler)
    try:
        with pytest.raises(ProviderError) as raised:
            await ModelAdapter(provider, max_zero_payload_retries=0).invoke(REQUEST, attempt_observer=observed.observe)
    finally:
        await provider.client.close()
    attempt = await recorder_db.scalar(select(ProviderAttempt))
    assert calls == 1 and wire.closed
    assert attempt.status == "failed" and not attempt.received_payload and attempt.first_payload_at is None
    assert attempt.provider_request_id == raised.value.provider_request_id == "header-id"
    assert json.loads(attempt.error_detail)["provider_identifiers"] == {"provider_request_id": "header-id", "provider_request_id_source": "header:x-request-id"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize("stage", ["text", "tool"])
async def test_partial_payload_failure_preserves_both_ids_without_replay(recorder_db, kind, stage):
    calls = 0
    wire = WireStream(sse(kind, stage), fail=True)
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "text/event-stream", "request-id": "request-id"}, stream=wire)
    provider = await provider_with_transport(kind, handler)
    try:
        with pytest.raises(ProviderError) as raised:
            await ModelAdapter(provider).invoke(REQUEST, attempt_observer=observed_attempts(recorder_db).observe)
    finally:
        await provider.client.close()
    attempt = await recorder_db.scalar(select(ProviderAttempt))
    assert calls == 1 and wire.closed
    assert raised.value.received_payload and attempt.received_payload and attempt.first_payload_at is not None
    assert attempt.status == "outcome_unknown" and not attempt.tool_call_observed and not attempt.side_effect_committed
    assert attempt.provider_request_id == "request-id"
    assert json.loads(attempt.error_detail)["provider_identifiers"] == {"provider_request_id": "request-id", "provider_request_id_source": "header:request-id", "provider_response_id": "body-id"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize("second_has_ids", [True, False])
async def test_metadata_only_retry_has_separate_attempt_ids_and_preserves_retry_budget(recorder_db, kind, second_has_ids):
    calls = 0
    wires = []
    def handler(request):
        nonlocal calls
        calls += 1
        first = calls == 1
        body_id = "body-first" if first else ("body-second" if second_has_ids else None)
        wire = WireStream(sse(kind, "metadata" if first else "text", body_id, finish=not first), fail=first)
        wires.append(wire)
        headers = {"content-type": "text/event-stream"}
        if first or second_has_ids:
            headers["x-request-id"] = "request-first" if first else "request-second"
        return httpx.Response(200, headers=headers, stream=wire)
    provider = await provider_with_transport(kind, handler)
    try:
        result = await ModelAdapter(provider).invoke(REQUEST, attempt_observer=observed_attempts(recorder_db).observe)
    finally:
        await provider.client.close()
    attempts = list(await recorder_db.scalars(select(ProviderAttempt).order_by(ProviderAttempt.attempt_no)))
    assert calls == 2 and all(wire.closed for wire in wires)
    assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
    assert [attempt.provider_request_id for attempt in attempts] == ["request-first", "request-second" if second_has_ids else None]
    assert not attempts[0].received_payload and attempts[0].first_payload_at is None
    assert attempts[1].received_payload and attempts[1].first_payload_at is not None
    assert result.provider_request_id == ("request-second" if second_has_ids else None)
    assert result.provider_response_id == ("body-second" if second_has_ids else None)
    assert "body-first" in attempts[0].error_detail


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_http_error_before_stream_object_keeps_header_and_sdk_does_not_hide_retries(recorder_db, kind):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"x-request-id": "failed-http-request"}, json={"error": {"type": "api_error", "message": "temporarily unavailable"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream", "x-request-id": "successful-http-request"}, stream=WireStream(sse(kind, "text", "completion", finish=True)))
    provider = await provider_with_transport(kind, handler)
    try:
        result = await ModelAdapter(provider).invoke(REQUEST, attempt_observer=observed_attempts(recorder_db).observe)
    finally:
        await provider.client.close()
    attempts = list(await recorder_db.scalars(select(ProviderAttempt).order_by(ProviderAttempt.attempt_no)))
    assert calls == 2 and len(attempts) == 2
    assert [attempt.provider_request_id for attempt in attempts] == ["failed-http-request", "successful-http-request"]
    assert result.provider_request_id == "successful-http-request" and result.provider_response_id == "completion"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize("header_id", ["valid" * 38 + "x", "x" * 192, "not an id"])
async def test_id_storage_boundaries_and_header_priority(recorder_db, kind, header_id):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream", "x-request-id": header_id}, stream=WireStream(sse(kind, "text", "completion", finish=True)))
    provider = await provider_with_transport(kind, handler)
    try:
        result = await ModelAdapter(provider).invoke(REQUEST, attempt_observer=observed_attempts(recorder_db).observe)
    finally:
        await provider.client.close()
    attempt = await recorder_db.scalar(select(ProviderAttempt))
    expected = header_id if len(header_id) == 191 else "completion"
    assert attempt.provider_request_id == result.provider_request_id == expected
    assert result.provider_request_id_source == ("header:x-request-id" if expected == header_id else "response_id_fallback")
    assert result.provider_response_id == "completion"


@pytest.mark.asyncio
async def test_late_metadata_does_not_reset_payload_timestamp_or_enable_replay(recorder_db):
    class Provider:
        calls = 0
        async def stream(self, *args):
            self.calls += 1
            yield LLMChunk(type="text_delta", content="already visible")
            yield LLMChunk(type="metadata", provider_request_id="late-header", provider_request_id_source="header:x-request-id", provider_response_id="message")
            raise httpx.RemoteProtocolError("incomplete chunked read")
    provider = Provider()
    observed = observed_attempts(recorder_db)
    payload_time = None
    async def observe(event):
        nonlocal payload_time
        await observed.observe(event)
        attempt = observed.attempts[event.attempt_no]
        if event.phase == "payload":
            payload_time = attempt.first_payload_at
        if event.phase == "metadata":
            assert event.received_payload and attempt.received_payload
            assert attempt.first_payload_at == payload_time and payload_time is not None
            assert attempt.ended_at is None and attempt.status == "running"
    with pytest.raises(ProviderError):
        await ModelAdapter(provider).invoke(REQUEST, attempt_observer=observe)
    assert provider.calls == 1
    attempt = observed.attempts[1]
    assert attempt.status == "outcome_unknown" and attempt.first_payload_at == payload_time
    assert attempt.provider_request_id == "late-header"


@pytest.mark.asyncio
async def test_openai_cleanup_failure_does_not_replace_original_stream_failure(recorder_db):
    class FailedCloseStream(WireStream):
        async def aclose(self):
            raise RuntimeError("cleanup failure must not mask provider failure")
    wire = FailedCloseStream(sse("openai", "text"), fail=True)
    provider = await provider_with_transport("openai", lambda request: httpx.Response(200, headers={"content-type": "text/event-stream", "x-request-id": "header"}, stream=wire))
    try:
        with pytest.raises(ProviderError) as raised:
            await ModelAdapter(provider).invoke(REQUEST, attempt_observer=observed_attempts(recorder_db).observe)
    finally:
        await provider.client.close()
    assert raised.value.code == "provider_transport_error"
    assert "incomplete chunked read" in str(raised.value)
    assert raised.value.provider_request_id == "header"
