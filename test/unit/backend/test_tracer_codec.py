"""Unit tests for Tracer gzip codec."""

import gzip
import json

import pytest

from app.runtime.tracer import _compress, _decompress

pytestmark = pytest.mark.unit


class TestTracerCodec:
    def test_roundtrip(self):
        data = {"messages": [{"role": "user", "content": "hello"}], "model": "test"}
        compressed = _compress(data)
        assert isinstance(compressed, bytes)
        assert _decompress(compressed) == data

    def test_nested_structures(self):
        data = {
            "steps": [{"id": "s1", "output": "result"}],
            "goal": "Test goal",
            "tokens": 42,
        }
        assert _decompress(_compress(data)) == data

    def test_gzip_magic_bytes(self):
        compressed = _compress({"key": "value"})
        assert compressed[:2] == b"\x1f\x8b"

    def test_non_ascii_content(self):
        data = {"content": "分析季度销售数据"}
        restored = _decompress(_compress(data))
        assert restored["content"] == "分析季度销售数据"
