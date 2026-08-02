"""Unit tests for OpenAI message normalization."""

import pytest

from app.core.model_hub.openai import normalize_messages_for_openai

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
