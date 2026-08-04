"""Unit tests for provider-neutral structured response contracts."""

import pytest

from app.core.model_hub.response_contracts import structured_response_format
from app.schemas.registry_v1 import ModelCreate

pytestmark = pytest.mark.unit


def test_json_object_contract_uses_compatible_wire_shape():
    assert structured_response_format(
        "structured.plan_proposal.v1", "json_object",
    ) == {"type": "json_object"}


def test_json_schema_contract_is_closed_and_requires_plan_fields():
    response_format = structured_response_format(
        "structured.plan_proposal.v1", "json_schema",
    )
    schema = response_format["json_schema"]["schema"]
    assert response_format["json_schema"]["strict"] is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"goal", "deliverable", "nodes", "dependencies"}


def test_conversation_completion_contract_has_closed_effect_vocabulary():
    response_format = structured_response_format(
        "structured.conversation_completion.v1", "json_schema",
    )
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"message", "required_tool_keys", "reason_code"}
    assert set(schema["properties"]["required_tool_keys"]["items"]["enum"]) >= {
        "request_clarification", "invoke_agent", "search_group_history",
        "execute", "write_file", "publish_artifact",
    }


def test_model_registry_rejects_unknown_structured_capability():
    with pytest.raises(ValueError, match="structured_output"):
        ModelCreate(
            key="invalid", provider="openai_compatible", model_name="model",
            capabilities={"structured_output": "best_effort"},
        )
