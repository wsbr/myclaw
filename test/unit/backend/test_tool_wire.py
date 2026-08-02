"""Unit tests: tool wire mapping is internalized at the model-call layer."""

import pytest

from app.core.model_hub.registry import ModelHub
from app.core.model_hub.tool_wire import seal_schema, to_anthropic_tools, to_openai_tools
from app.core.tool_registry.builtin.invoke_agent import InvokeAgentTool

pytestmark = pytest.mark.unit


SAMPLE = {
    "name": "demo",
    "description": "desc",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "delay": {"type": "string"},
        },
        "required": ["title"],
    },
}


class TestToolAuthorsStayPlain:
    def test_builtin_definition_has_no_protocol_flags(self):
        defn = InvokeAgentTool().to_llm_definition()
        assert "strict" not in defn
        assert "additionalProperties" not in defn["input_schema"]


class TestModelCallWire:
    def test_seal_closes_objects(self):
        sealed = seal_schema(SAMPLE["input_schema"])
        assert sealed["additionalProperties"] is False

    def test_openai_compatible_call_seals_without_strict(self):
        wire = to_openai_tools([SAMPLE], strict=False)[0]
        fn = wire["function"]
        assert "strict" not in fn
        assert fn["parameters"]["additionalProperties"] is False
        assert fn["parameters"]["required"] == ["title"]

    def test_openai_official_call_adds_strict(self):
        wire = to_openai_tools([SAMPLE], strict=True)[0]
        fn = wire["function"]
        assert fn["strict"] is True
        assert "delay" in fn["parameters"]["required"]

    def test_anthropic_call_adds_strict_when_capable(self):
        wire = to_anthropic_tools([SAMPLE], strict=True)[0]
        assert wire["strict"] is True
        assert wire["input_schema"]["additionalProperties"] is False


class TestModelHubWiresCapabilityIntoCaller:
    def test_compatible_provider_disables_strict(self):
        assert ModelHub.get({}, "openai_compatible", "k").tool_strict is False

    def test_openai_enables_strict(self):
        assert ModelHub.get({}, "openai", "k").tool_strict is True

    def test_anthropic_enables_strict(self):
        assert ModelHub.get({}, "anthropic", "k").tool_strict is True
