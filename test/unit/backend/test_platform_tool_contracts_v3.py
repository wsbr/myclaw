from __future__ import annotations

import json
from types import SimpleNamespace

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
from app.core.tool_registry.builtin import web_tools
from app.core.tool_registry.builtin.browser_tools import _failure as browser_failure
from app.core.tool_registry.builtin.execute import _bounded_execution_argv
from app.workers.sandbox_worker import (
    _attach_memory_disposition, _explicitly_proves_side_effect_not_started,
)
from app.services.artifact_service import artifact_publication_key
from app.services.tool_connection_service import (
    ToolConnectionError,
    _assert_public_config,
    _assert_public_endpoint,
    _validate_connection_config,
    validate_connection_endpoint,
)
from app.services import web_tool_service
from app.services.web_tool_service import (
    WebToolError, _public_address, _trusted_synthetic_address, _validate_url,
)
from app.runtime.coordination_tools import dispatch_coordination_tool
from app.runtime.snapshot_resolver import _eager_platform_tool_keys
from app.services.browser_worker_service import BrowserOutcomeUnknown, BrowserWorkerError
from app.services.registry_reconcile import builtin_tool_specs
from app.services.tool_display_names import mcp_tool_display_name_zh
from app.config import settings
from app.config import Settings
from app.schemas.registry_v1 import ModelVersionCreate


pytestmark = pytest.mark.unit


def test_every_builtin_tool_has_an_intentional_chinese_display_name():
    specs = builtin_tool_specs()

    assert {spec.tool_key for spec in specs} == set(BUILTIN_TOOLS)
    assert all(spec.display_name_zh for spec in specs)
    assert all(any("\u3400" <= char <= "\u9fff" for char in spec.display_name_zh) for spec in specs)


def test_mcp_chinese_display_name_does_not_copy_unknown_english_titles():
    assert mcp_tool_display_name_zh("search_openai_docs", "Search OpenAI Docs") == "搜索 OpenAI 文档"
    assert mcp_tool_display_name_zh("native_chinese", "原生中文工具") == "原生中文工具"
    assert mcp_tool_display_name_zh("unknown_tool", "Unknown Tool") == ""
    assert mcp_tool_display_name_zh(
        "unknown_tool",
        "Unknown Tool",
        current_display_name="Previous English Title",
        current_display_name_zh="管理员自定义名称",
    ) == "管理员自定义名称"


def test_active_browser_session_keeps_exact_browser_contracts_eager_across_turns():
    cold = _eager_platform_tool_keys(browser_session_active=False)
    warm = _eager_platform_tool_keys(browser_session_active=True)
    browser_keys = {
        "browser_navigate", "browser_observe", "browser_interact", "browser_screenshot",
    }
    assert cold & browser_keys == {"browser_navigate"}
    assert {"memory_search", "memory_propose"} <= cold
    assert "delete_path" in cold
    assert browser_keys <= warm


def test_browser_preinteraction_conflicts_are_not_misclassified_as_outcome_unknown():
    conflict = browser_failure(BrowserWorkerError(
        "observe again", code="browser_revision_conflict", category="conflict",
    ))
    unknown = browser_failure(BrowserOutcomeUnknown("worker stopped"))

    assert conflict.status == "failed"
    assert conflict.error.details == {"side_effect_started": False}
    assert unknown.status == "outcome_unknown"
    assert unknown.error.details == {}


def test_memory_propose_result_reports_atomic_trusted_arbitration_disposition():
    result = ToolResult(
        status="succeeded", summary="candidate created",
        data={"candidate_id": "candidate", "status": "pending"},
        evidence=[{"kind": "user_messages", "message_ids": ["message"]}],
    )
    candidate = SimpleNamespace(
        id="candidate", status="accepted", result_memory_id="memory",
        rejection_code=None,
    )

    settled = _attach_memory_disposition(result, candidate)

    assert settled.data["status"] == "accepted"
    assert settled.data["memory_id"] == "memory"
    assert settled.content_blocks == [{"type": "json", "value": settled.data}]
    assert settled.evidence[-1]["kind"] == "memory_arbitration"


def test_memory_search_contract_allows_bounded_inventory_recovery():
    tool = BUILTIN_TOOLS["memory_search"]
    assert tool.input_schema["properties"]["query"]["minLength"] == 0
    assert "empty query" in tool.description


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
        "tenant_id": "test-tenant",
        "session_id": "session", "runner_id": "runner",
        "user_id": "user", "agent_id": "agent",
    }
    first = RuntimeContext(**values)
    second = RuntimeContext(**values)
    first.call_chain.append("child")
    assert second.call_chain == []


@pytest.mark.asyncio
async def test_search_tools_reports_authorized_but_plan_blocked_versions_as_discoverable():
    capability = SimpleNamespace(
        id="capability",
        authorized_tool_versions=[{
            "name": "write_file",
            "namespace": "platform",
            "description": "Write a file in the governed Workspace",
            "tool_version_id": "tool-version",
        }],
        effective_tool_keys=[],
        denied_capabilities=[{
            "capability": "write_file",
            "reason_code": "permission_mode_readonly",
        }],
    )

    class Database:
        async def scalar(self, _statement):
            return capability

    result, durable_wait, wait_reason = await dispatch_coordination_tool(
        "search_tools", {"query": "write file"},
        context=RuntimeContext(
            tenant_id="test-tenant",
            session_id="session", runner_id="runner",
            user_id="user", agent_id="agent",
            permission_mode="plan_readonly",
        ),
        db=Database(), run_id="run", work_item_id="work",
        source_turn_id="turn", provider_call_id="call",
    )

    assert result.success is True
    assert durable_wait is False and wait_reason is None
    assert result.data["tools"] == [{
        "name": "write_file",
        "description": "Write a file in the governed Workspace",
        "tool_version_id": "tool-version",
        "execution_available": False,
        "blocked_reason": "permission_mode_readonly",
    }]


@pytest.mark.asyncio
async def test_search_tools_empty_query_can_inventory_the_authorized_catalog():
    capability = SimpleNamespace(
        id="capability",
        authorized_tool_versions=[{
            "name": "publish_artifact", "namespace": "platform",
            "description": "Publish an immutable Artifact",
            "tool_version_id": "publish-version",
        }, {
            "name": "write_file", "namespace": "platform",
            "description": "Write a Workspace file",
            "tool_version_id": "write-version",
        }],
        effective_tool_keys=[],
        denied_capabilities=[
            {"capability": "publish_artifact", "reason_code": "permission_mode_readonly"},
            {"capability": "write_file", "reason_code": "permission_mode_readonly"},
        ],
    )

    class Database:
        async def scalar(self, _statement):
            return capability

    result, _, _ = await dispatch_coordination_tool(
        "search_tools", {"query": "", "limit": 50},
        context=RuntimeContext(
            tenant_id="test-tenant",
            session_id="session", runner_id="runner",
            user_id="user", agent_id="agent",
            permission_mode="plan_readonly",
        ),
        db=Database(), run_id="run", work_item_id="work",
        source_turn_id="turn", provider_call_id="call",
    )

    assert [item["name"] for item in result.data["tools"]] == [
        "publish_artifact", "write_file",
    ]
    assert all(item["execution_available"] is False for item in result.data["tools"])


@pytest.mark.asyncio
async def test_web_fetch_uses_platform_owned_balanced_capture_limits(monkeypatch):
    captured: dict = {}

    async def fake_safe_fetch(url: str, **kwargs):
        captured.update({"url": url, **kwargs})
        return {
            "final_url": url,
            "title": "Example",
            "mime_type": "text/html",
            "text": "bounded",
            "checksum": "a" * 64,
            "elapsed_ms": 1,
            "text_length": 250_000,
            "truncated": True,
        }

    monkeypatch.setattr(web_tools, "safe_fetch", fake_safe_fetch)
    result = await BUILTIN_TOOLS["web_fetch"].execute(
        {"url": "https://example.com/large-document"},
        RuntimeContext(
            tenant_id="test-tenant",
            session_id="session", runner_id="runner",
            user_id="user", agent_id="agent",
        ),
    )

    assert result.success is True
    assert captured["max_text_chars"] == 100_000
    assert captured["max_bytes"] == 2 * 1024 * 1024
    assert captured["objective"] == "Extract the main factual content from this source."
    assert set(BUILTIN_TOOLS["web_fetch"].input_schema["properties"]) == {"url", "objective"}
    assert BUILTIN_TOOLS["web_fetch"].input_schema["required"] == ["url", "objective"]


def test_execute_applies_memory_limit_after_the_worker_python_image_is_replaced(monkeypatch):
    monkeypatch.setattr(
        "app.core.tool_registry.builtin.execute.PRLIMIT_PATH",
        "/usr/bin/prlimit",
    )
    assert _bounded_execution_argv(
        ["sha256sum", "reports/platform-uat.txt"], memory_mb=256,
    ) == [
        "/usr/bin/prlimit", "--as=268435456", "--",
        "sha256sum", "reports/platform-uat.txt",
    ]


def test_non_idempotent_failure_can_explicitly_prove_the_process_never_started():
    result = ToolResult(
        status="failed", summary="isolation unavailable",
        error="process was not started",
        error_code="execute_isolation_unavailable",
        error_category="authorization",
        error_details={"side_effect_started": False},
    )
    assert _explicitly_proves_side_effect_not_started(result) is True
    uncertain = ToolResult(
        status="failed", summary="provider disconnected",
        error="lost response", error_category="provider",
    )
    assert _explicitly_proves_side_effect_not_started(uncertain) is False


def test_artifact_publication_key_is_stable_and_version_sensitive():
    values = {
        "session_id": "session", "user_id": "user", "agent_id": "agent",
        "source_relative_path": "reports/result.txt", "checksum": "a" * 64,
        "name": "result.txt", "mime_type": "text/plain",
        "parent_artifact_id": None, "group_session_id": None,
    }
    first = artifact_publication_key(**values)
    assert first == artifact_publication_key(**values)
    assert len(first) == 64
    assert first != artifact_publication_key(
        **{**values, "parent_artifact_id": "parent-version"},
    )


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


def test_bailian_connection_config_is_typed_and_bounded():
    _validate_connection_config("bailian", {"model": "qwen-plus", "search_strategy": "turbo"})
    _validate_connection_config("bailian", {})
    with pytest.raises(ToolConnectionError, match="unsupported fields"):
        _validate_connection_config("bailian", {"enable_search": True})
    with pytest.raises(ToolConnectionError, match="search_strategy"):
        _validate_connection_config("bailian", {"search_strategy": "unbounded"})


@pytest.mark.asyncio
async def test_bailian_search_uses_forced_native_search_and_only_structured_evidence(monkeypatch):
    captured = {}

    async def public_dns(_hostname, _port):
        return ["203.0.113.20"]

    async def bounded(_client, method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return ({
            "request_id": "req-bailian-real",
            "output": {
                "choices": [{"message": {"content": "Generated prose is not evidence."}}],
                "search_info": {"search_results": [{
                    "title": "Official result", "url": "https://example.com/source",
                    "content": "Structured provider snippet", "publish_time": "2026-08-10",
                }]},
            },
            "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
        }, SimpleNamespace(headers={"x-request-id": "header-request"}))

    monkeypatch.setattr(web_tool_service, "_resolve_public", public_dns)
    monkeypatch.setattr(web_tool_service, "_bounded_json_response", bounded)
    items, provider = await web_tool_service.web_search(
        provider="bailian",
        endpoint="https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
        api_key="write-only-test-key", query="current public evidence", count=5,
        freshness="pw", country=None,
        config={"model": "qwen-plus", "search_strategy": "turbo"},
    )

    request = captured["json"]
    assert captured["method"] == "POST"
    assert request["parameters"]["enable_search"] is True
    assert request["parameters"]["max_tokens"] == 64
    assert request["parameters"]["temperature"] == 0
    assert request["parameters"]["search_options"] == {
        "forced_search": True, "enable_source": True,
        "search_strategy": "turbo", "freshness": 7,
    }
    assert len(items) == 1
    assert items[0] == {
        "title": "Official result", "url": "https://example.com/source",
        "snippet": "Structured provider snippet", "published_at": "2026-08-10",
        "source_id": items[0]["source_id"], "site_name": "example.com",
    }
    assert items[0]["source_id"].startswith("web-")
    assert "Generated prose" not in json.dumps(items)
    assert provider == {
        "name": "bailian", "request_id": "req-bailian-real",
        "latency_ms": provider["latency_ms"], "model": "qwen-plus",
        "search_strategy": "turbo",
        "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
    }


@pytest.mark.asyncio
async def test_bailian_past_day_uses_query_constraint_not_unsupported_numeric_freshness(monkeypatch):
    captured = {}

    async def public_dns(_hostname, _port):
        return ["203.0.113.20"]

    async def bounded(_client, _method, _url, **kwargs):
        captured.update(kwargs)
        return ({
            "output": {"search_info": {"search_results": [{
                "title": "Recent result", "url": "https://example.com/recent",
            }]}},
            "usage": {"total_tokens": 1},
        }, SimpleNamespace(headers={"x-request-id": "req-pd"}))

    monkeypatch.setattr(web_tool_service, "_resolve_public", public_dns)
    monkeypatch.setattr(web_tool_service, "_bounded_json_response", bounded)
    await web_tool_service.web_search(
        provider="bailian",
        endpoint="https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
        api_key="write-only-test-key", query="Chromium update", count=5,
        freshness="pd", country=None,
        config={"model": "qwen-plus", "search_strategy": "turbo"},
    )

    request = captured["json"]
    assert "freshness" not in request["parameters"]["search_options"]
    assert "过去24小时" in request["input"]["messages"][0]["content"]
    assert "Chromium update" in request["input"]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_bailian_search_rejects_successful_model_answer_without_search_evidence(monkeypatch):
    async def public_dns(_hostname, _port):
        return ["203.0.113.20"]

    async def bounded(_client, _method, _url, **_kwargs):
        return ({
            "output": {"choices": [{"message": {"content": "plausible but ungrounded"}}]},
        }, SimpleNamespace(headers={}))

    monkeypatch.setattr(web_tool_service, "_resolve_public", public_dns)
    monkeypatch.setattr(web_tool_service, "_bounded_json_response", bounded)
    with pytest.raises(WebToolError) as observed:
        await web_tool_service.web_search(
            provider="bailian",
            endpoint="https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
            api_key="write-only-test-key", query="current public evidence", count=5,
            freshness=None, country=None, config={},
        )
    assert observed.value.code == "provider_search_not_executed"


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
