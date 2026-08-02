import pytest

from app.core.tool_registry.base import ToolResult


pytestmark = pytest.mark.unit


def test_legacy_success_constructor_serializes_documented_envelope():
    result = ToolResult(success=True, data={"value": 1})

    assert result.success is True
    assert result.model_dump(mode="json") == {
        "status": "succeeded",
        "data": {"value": 1},
        "summary": "Tool completed",
        "artifact_ids": [],
        "provider_request_id": None,
        "retryable": False,
        "error": None,
    }


def test_legacy_error_constructor_serializes_structured_error():
    result = ToolResult(
        success=False,
        error="path is outside the Workspace",
        error_code="invalid_path",
        error_category="validation",
    )

    assert result.success is False
    assert result.error.code == "invalid_path"
    assert result.model_dump(mode="json")["error"] == {
        "code": "invalid_path",
        "message": "path is outside the Workspace",
        "category": "validation",
        "details": {},
    }
