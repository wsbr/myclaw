"""Unit tests for OpenAI message normalization."""

from types import SimpleNamespace

import pytest

from app.core.model_hub.openai import OpenAILLM, normalize_messages_for_openai

pytestmark = pytest.mark.unit


class TestNormalizeMessagesForOpenAI:
    def test_converts_internal_tool_use_format(self):
        messages = [
            {"role": "user", "content": "5分钟后提醒我"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "tool_use",
                        "name": "write_file",
                        "input": {"path": "note.txt", "content": "reminder"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_use_id": "call_1",
                "content": '{"path": "note.txt"}',
            },
        ]

        result = normalize_messages_for_openai(messages)

        assert result[1]["tool_calls"] == [{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": '{"path": "note.txt", "content": "reminder"}',
            },
        }]
        assert result[2] == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"path": "note.txt"}',
        }

    def test_leaves_openai_format_unchanged(self):
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": '{"q": "test"}',
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "call_2", "content": "ok"},
        ]

        result = normalize_messages_for_openai(messages)
        assert result == messages

    def test_passes_through_plain_messages(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        assert normalize_messages_for_openai(messages) == messages


@pytest.mark.asyncio
async def test_stream_forwards_frozen_temperature_and_output_limit():
    captured = {}

    class Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)

            async def chunks():
                yield SimpleNamespace(usage=None, choices=[])

            return chunks()

    model = OpenAILLM("unused-test-key")
    await model.client.close()
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    assert [chunk async for chunk in model.stream(
        [{"role": "user", "content": "return json"}], None,
        {"model_id": "model", "temperature": 0, "max_tokens": 4096},
    )] == []
    assert captured["temperature"] == 0
    assert captured["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_stream_retries_one_transient_failure_only_before_output(monkeypatch):
    calls = 0

    class Completions:
        async def create(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("Request timed out.")

            async def chunks():
                yield SimpleNamespace(
                    usage=None,
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="ok", tool_calls=None))],
                )

            return chunks()

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.core.model_hub.openai.asyncio.sleep", no_sleep)
    model = OpenAILLM("unused-test-key")
    await model.client.close()
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    chunks = [chunk async for chunk in model.stream(
        [{"role": "user", "content": "return json"}], None, {"model_id": "model"},
    )]
    assert calls == 2
    assert [chunk.content for chunk in chunks] == ["ok"]


@pytest.mark.asyncio
async def test_stream_retries_incomplete_chunked_read_before_output(monkeypatch):
    calls = 0

    class Completions:
        async def create(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError(
                    "peer closed connection without sending complete message body "
                    "(incomplete chunked read)"
                )

            async def chunks():
                yield SimpleNamespace(
                    usage=None,
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="recovered", tool_calls=None))],
                )

            return chunks()

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.core.model_hub.openai.asyncio.sleep", no_sleep)
    model = OpenAILLM("unused-test-key")
    await model.client.close()
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    chunks = [chunk async for chunk in model.stream(
        [{"role": "user", "content": "return json"}], None, {"model_id": "model"},
    )]
    assert calls == 2
    assert [chunk.content for chunk in chunks] == ["recovered"]


@pytest.mark.asyncio
async def test_stream_never_retries_after_a_delta():
    calls = 0

    class Completions:
        async def create(self, **kwargs):
            nonlocal calls
            calls += 1

            async def chunks():
                yield SimpleNamespace(
                    usage=None,
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="partial", tool_calls=None))],
                )
                raise RuntimeError("Request timed out.")

            return chunks()

    model = OpenAILLM("unused-test-key")
    await model.client.close()
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    received = []
    with pytest.raises(RuntimeError, match="timed out"):
        async for chunk in model.stream(
            [{"role": "user", "content": "return json"}], None, {"model_id": "model"},
        ):
            received.append(chunk.content)
    assert calls == 1
    assert received == ["partial"]
