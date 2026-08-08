from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.core.platform_secrets import (
    ENCRYPTED_SECRET_PREFIX,
    PlatformSecretError,
    decrypt_secret,
    encrypt_secret,
)
from app.core.model_hub.tool_wire import openai_strict_schema, to_anthropic_tools
from app.core.tool_registry.base import RuntimeContext, ToolResult
from app.core.tool_registry.registry import BUILTIN_TOOLS
from app.core.tool_registry.schema import (
    BUILTIN_RESULT_DATA_SCHEMAS,
    TOOL_RESULT_SCHEMA,
    assert_valid_schema,
    builtin_result_schema,
    validation_error_refs,
    validation_errors,
)
from app.core.tool_registry.builtin.workspace_catalog import (
    _compile_search_pattern, _parse_unified_diff,
)
from app.services.tool_connection_service import (
    ToolConnectionError,
    _assert_public_config,
    _assert_public_endpoint,
    validate_connection_endpoint,
)
from app.services.web_tool_service import (
    WebToolError, _public_address, _trusted_synthetic_address, _validate_url,
)
from app.config import settings
from app.config import Settings
from app.schemas.registry_v1 import ModelVersionCreate


pytestmark = pytest.mark.unit


def test_every_builtin_has_a_concrete_draft_2020_12_contract_and_recovery_description():
    assert set(BUILTIN_TOOLS) == set(BUILTIN_RESULT_DATA_SCHEMAS)
    for key, tool in BUILTIN_TOOLS.items():
        assert_valid_schema(tool.input_schema, label=f"{key} input")
        output = builtin_result_schema(key)
        assert_valid_schema(output, label=f"{key} output")
        assert output["properties"]["data"] != {}
        description = tool.description
        assert all(label in description for label in ("WHEN:", "WHEN NOT:", "RETURNS:", "RECOVERY:"))


def test_result_envelope_requires_all_stable_fields_and_typed_errors():
    good = ToolResult(status="succeeded", summary="done", data={}).model_dump(mode="json")
    assert not validation_errors(good, TOOL_RESULT_SCHEMA)
    missing = dict(good)
    missing.pop("evidence")
    assert any(item["validator"] == "required" for item in validation_errors(missing, TOOL_RESULT_SCHEMA))
    bad_error = ToolResult(
        status="failed",
        summary="bad",
        error="bad",
        error_code="bad",
        error_category="validation",
    ).model_dump(mode="json")
    bad_error["error"]["category"] = "unknown"
    assert validation_errors(bad_error, TOOL_RESULT_SCHEMA)

    succeeded_with_error = dict(good)
    succeeded_with_error["error"] = {
        "code": "impossible", "message": "impossible",
        "category": "provider", "details": {},
    }
    assert validation_errors(succeeded_with_error, TOOL_RESULT_SCHEMA)
    failed_without_error = dict(good)
    failed_without_error["status"] = "failed"
    assert validation_errors(failed_without_error, TOOL_RESULT_SCHEMA)
    with pytest.raises(ValidationError):
        ToolResult(status="succeeded", summary="bad", error="must not coexist")


def test_untrusted_output_validation_evidence_does_not_reflect_rejected_values():
    secret = "provider-body-must-never-enter-trace"
    issues = validation_error_refs(secret, {"type": "integer"})
    assert issues == [{
        "path": "/", "schema_path": "/type", "validator": "type",
    }]
    assert secret not in json.dumps(issues)


def test_runtime_context_call_chains_are_not_shared_between_runs():
    values = {
        "session_id": "session", "runner_id": "runner",
        "user_id": "user", "agent_id": "agent",
    }
    first = RuntimeContext(**values)
    second = RuntimeContext(**values)
    first.call_chain.append("child")
    assert second.call_chain == []


def test_openai_strict_projects_optional_fields_as_nullable_but_anthropic_keeps_optional_presence():
    neutral = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "required_value": {"type": "string"},
            "optional_value": {"type": "integer", "minimum": 1},
        },
        "required": ["required_value"],
        "additionalProperties": False,
        "if": {"properties": {"required_value": {"const": "x"}}},
        "then": {"required": ["optional_value"]},
    }
    projected = openai_strict_schema(neutral)
    assert projected["required"] == ["required_value", "optional_value"]
    assert projected["properties"]["optional_value"] == {
        "anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}],
    }
    assert "if" not in projected and "then" not in projected
    anthropic = to_anthropic_tools([{
        "name": "example", "description": "example", "input_schema": neutral,
    }], strict=True)[0]["input_schema"]
    assert anthropic["required"] == ["required_value"]
    assert anthropic["properties"]["optional_value"] == {"type": "integer", "minimum": 1}


def test_platform_secret_is_randomized_authenticated_ciphertext_with_only_a_fingerprint_for_identity():
    plaintext = "provider-key-used-only-in-this-test"
    first, fingerprint = encrypt_secret(plaintext, purpose="test-provider")
    second, second_fingerprint = encrypt_secret(plaintext, purpose="test-provider")
    assert first.startswith(ENCRYPTED_SECRET_PREFIX)
    assert first != second
    assert fingerprint == second_fingerprint
    assert plaintext not in json.dumps({"ciphertext": first, "fingerprint": fingerprint})
    assert decrypt_secret(first, purpose="test-provider") == plaintext
    with pytest.raises(PlatformSecretError):
        decrypt_secret(first, purpose="different-purpose")


@pytest.mark.parametrize("endpoint", [
    "http://api.search.brave.com/res/v1/web/search",
    "https://user:password@example.com/search",
    "https://api.example.com/mcp?api_key=plaintext",
    "file:///etc/passwd",
])
def test_admin_connection_endpoint_rejects_cleartext_credentials_and_non_https(endpoint: str):
    with pytest.raises(ToolConnectionError):
        validate_connection_endpoint(endpoint, connection_type="brave")


@pytest.mark.parametrize("endpoint", [
    "http://api.example.com/v1",
    "https://api.example.com/v1?token=plaintext",
    "https://user:password@api.example.com/v1",
])
def test_model_endpoint_never_transports_or_embeds_platform_credentials(endpoint: str):
    with pytest.raises(ValueError):
        ModelVersionCreate(
            provider="openai_compatible", model_name="real-provider-model",
            endpoint=endpoint, api_key="write-only-value",
            capabilities={"native_tool_calling": True},
        )


@pytest.mark.parametrize("field,value", [
    ("default_parameters", {"nested": {"api_key": "plaintext"}}),
    ("capabilities", {"provider": {"access-token": "plaintext"}}),
])
def test_model_public_snapshots_cannot_embed_credentials(field: str, value: dict):
    payload = {
        "provider": "openai_compatible", "model_name": "real-provider-model",
        "endpoint": "https://api.example.com/v1", "api_key": "write-only-value",
        "default_parameters": {},
        "capabilities": {"native_tool_calling": True},
    }
    payload[field] = value
    with pytest.raises(ValidationError, match="write-only api_key"):
        ModelVersionCreate(**payload)


@pytest.mark.parametrize("config", [
    {"api_key": "plaintext"},
    {"nested": {"access-token": "plaintext"}},
    {"items": [{"password": "plaintext"}]},
])
def test_connection_public_config_cannot_hide_plaintext_credentials(config: dict):
    with pytest.raises(ToolConnectionError):
        _assert_public_config(config)


def test_connection_public_config_allows_non_secret_token_budgets():
    _assert_public_config({"max_tokens": 1000, "protocol_version": "2026-07-28"})


@pytest.mark.asyncio
async def test_connection_endpoint_rejects_private_dns_resolution(monkeypatch):
    async def private_getaddrinfo(*_args, **_kwargs):
        return [(None, None, None, None, ("169.254.169.254", 443))]

    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", private_getaddrinfo)
    with pytest.raises(ToolConnectionError, match="prohibited network"):
        await _assert_public_endpoint(
            "https://metadata.example.invalid/mcp", connection_type="mcp",
        )


@pytest.mark.parametrize("value", [
    "http://localhost/private",
    "https://metadata.google.internal/computeMetadata/v1/",
    "https://service.corp.internal/private",
    "https://user:password@example.com/",
    "file:///etc/passwd",
])
def test_fetch_url_policy_rejects_local_names_credentials_and_non_http(value: str):
    with pytest.raises(WebToolError):
        _validate_url(value)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"])
def test_private_and_metadata_addresses_are_not_public(address: str):
    assert _public_address(address) is False


def test_synthetic_dns_gateway_is_explicit_and_never_allows_literal_ip(monkeypatch):
    monkeypatch.setattr(settings, "trusted_synthetic_dns_cidrs", ["198.18.0.0/15"])
    assert _trusted_synthetic_address("198.18.0.91", "www.rfc-editor.org") is True
    assert _trusted_synthetic_address("198.18.0.91", "198.18.0.91") is False


def test_invalid_synthetic_dns_cidr_is_rejected_at_configuration_boundary():
    with pytest.raises(ValueError, match="invalid CIDR"):
        Settings(trusted_synthetic_dns_cidrs=["198.18.0.1/15"])


def test_unified_diff_parser_preserves_no_newline_semantics():
    parsed = _parse_unified_diff(
        "--- a/note.txt\n"
        "+++ b/note.txt\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "\\ No newline at end of file\n"
        "+after\n"
        "\\ No newline at end of file\n"
    )
    assert parsed == [("note.txt", [(1, ["before"], ["after"])])]


@pytest.mark.parametrize("pattern", [
    r"(a+)+$", r"(a|aa)+$", r"(a+){2,}", r"(a)\\1", r"(?=a)a",
])
def test_workspace_regex_rejects_high_cost_or_context_dependent_constructs(pattern: str):
    with pytest.raises(Exception, match="prohibited"):
        _compile_search_pattern(pattern, regex=True, case_sensitive=True)
