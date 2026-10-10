"""Executable conformance suite for Agent-native model providers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.model_hub.base import BaseLLM, LLMChunk, LLMResponse
from app.core.model_hub.parameters import runtime_model_defaults
from app.core.model_hub.registry import ModelHub
from app.models.common import utcnow, uuid7
from app.models.registry import ConformanceRun, ModelVersion
from app.services.embedding_service import (
    OpenAICompatibleEmbeddingClient,
    embedding_spec,
    is_embedding_model,
)
from app.services.provider_credentials import (
    resolve_credential,
    resolve_provider_credential,
)
from app.runtime.model_adapter import ModelAdapter
from app.runtime.context_assembler import ContextAssembler
from app.runtime.coordination_tools import RUNTIME_COORDINATION_DEFINITIONS
from app.runtime.protocol import ProviderError, ProviderRequest, StopReason


PROBE_TOOL = {
    "name": "myclaw_conformance_echo",
    "description": "Return the provided value unchanged for protocol conformance testing.",
    "input_schema": {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
}
SECOND_PROBE_TOOL = {
    **PROBE_TOOL,
    "name": "myclaw_conformance_echo_second",
}
TYPED_REPAIR_PROBE_TOOL = {
    "name": "myclaw_conformance_typed_repair",
    "description": (
        "Record an integer occurrence count for Tool schema repair conformance testing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"occurrences": {"type": "integer", "minimum": 1}},
        "required": ["occurrences"],
        "additionalProperties": False,
    },
}

COORDINATION_PROBE_TOOLS = tuple({
    "name": item["name"],
    "description": item["description"],
    "input_schema": item["input_schema"],
} for item in RUNTIME_COORDINATION_DEFINITIONS if item["name"] in {
    "update_plan", "ask_user", "invoke_agent",
})


class _ZeroPayloadOnce(BaseLLM):
    """Fault-injection proxy proving retry occurs before the real Provider call."""

    def __init__(self, downstream: BaseLLM) -> None:
        self.downstream = downstream
        self.calls = 0

    async def chat(self, messages, tools, params) -> LLMResponse:
        return await self.downstream.chat(messages, tools, params)

    async def stream(self, messages, tools, params) -> AsyncIterator[LLMChunk]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporarily unavailable before response payload")
        async for chunk in self.downstream.stream(messages, tools, params):
            yield chunk

    async def count_tokens(self, messages, tools=None, params=None):
        return await self.downstream.count_tokens(messages, tools, params)


class _PayloadThenError(BaseLLM):
    """Fault injector proving a received wire event fences transparent replay."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools, params) -> LLMResponse:
        raise NotImplementedError

    async def stream(self, messages, tools, params) -> AsyncIterator[LLMChunk]:
        self.calls += 1
        yield LLMChunk(type="transport_payload")
        raise RuntimeError("transport failed after provider payload")

    async def count_tokens(self, messages, tools=None, params=None):
        return 0


@dataclass(frozen=True)
class ConformanceReport:
    passed: bool
    probes: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "probes": list(self.probes)}


def _hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _forced_tool_parameters(
    base_parameters: dict[str, Any], tool_choice: str | dict[str, Any],
) -> dict[str, Any]:
    """Build a forced-choice probe without changing the frozen runtime mode.

    Some OpenAI-compatible reasoning modes support native Tool calls in auto
    mode but reject the wire-level ``tool_choice`` field.  Their documented
    non-thinking mode supports forced choice.  Conformance therefore exercises
    the exact frozen reasoning mode for ordinary streaming/continuation probes
    and, only for a forced-choice protocol probe, uses the provider's compatible
    non-thinking variant.  Runtime Agent turns continue to use the immutable
    ModelVersion defaults unchanged.
    """
    parameters = dict(base_parameters)
    extra_body = parameters.get("extra_body")
    if isinstance(extra_body, dict) and extra_body.get("enable_thinking") is True:
        parameters["extra_body"] = {**extra_body, "enable_thinking": False}
    parameters["tool_choice"] = tool_choice
    return parameters


def _base_probe_parameters(version: ModelVersion) -> dict[str, Any]:
    defaults = runtime_model_defaults(version.default_parameters or {})
    return {
        **defaults,
        "model_id": version.model_name,
        "temperature": 0,
        # Keep the frozen runtime output budget up to the bounded UAT ceiling.
        # Reasoning tokens count against this Provider field, so a legacy 512
        # probe cap can suppress the visible continuation despite a valid call.
        "max_tokens": min(4096, int(defaults.get("max_tokens", 512))),
    }


class ProviderConformanceSuite:
    def __init__(self, provider: BaseLLM) -> None:
        self.provider = provider
        self.adapter = ModelAdapter(provider)

    async def run(self, version: ModelVersion) -> ConformanceReport:
        probes: list[dict[str, Any]] = []
        capabilities = dict(version.capabilities or {})
        mandatory = {
            "streaming": True,
            "native_tool_calling": True,
            "tool_result_continuation": True,
            "stop_reason": True,
        }
        missing = [key for key, expected in mandatory.items() if capabilities.get(key) is not expected]
        probes.append({
            "name": "declared_capabilities", "passed": not missing,
            "details": {"missing_or_false": missing},
        })
        if missing:
            return ConformanceReport(False, tuple(probes))

        limit_errors = []
        for key in ("context_window", "max_input_tokens", "max_output_tokens"):
            value = capabilities.get(key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                limit_errors.append(f"{key}_invalid")
        context_window = capabilities.get("context_window")
        max_input = capabilities.get("max_input_tokens")
        max_output = capabilities.get("max_output_tokens")
        if context_window and max_input and max_input > context_window:
            limit_errors.append("max_input_exceeds_context")
        if context_window and max_output and max_output > context_window:
            limit_errors.append("max_output_exceeds_context")
        frozen_output = (version.default_parameters or {}).get(
            "max_output_tokens",
            (version.default_parameters or {}).get("max_tokens", 8192),
        )
        if (
            isinstance(frozen_output, bool)
            or not isinstance(frozen_output, int)
            or frozen_output <= 0
        ):
            limit_errors.append("frozen_output_invalid")
        elif max_output and frozen_output > max_output:
            limit_errors.append("frozen_output_exceeds_capability")
        probes.append({
            "name": "declared_context_limits",
            "passed": not limit_errors,
            "details": {
                "errors": limit_errors,
                "context_window": context_window,
                "max_input_tokens": max_input,
                "max_output_tokens": max_output,
                "frozen_output_tokens": frozen_output,
            },
        })
        if limit_errors:
            return ConformanceReport(False, tuple(probes))

        base_parameters = _base_probe_parameters(version)
        zero_payload_proxy = _ZeroPayloadOnce(self.provider)
        plain = await ModelAdapter(zero_payload_proxy).invoke(ProviderRequest(
            messages=({
                "role": "user",
                "content": "MyClaw 兼容性探针：请用中文返回一条普通 Assistant Message，不要调用 Tool。",
            },),
            tools=(), parameters=base_parameters,
        ))
        plain_passed = bool(plain.assistant_messages) and not plain.tool_calls and plain.stop_reason in {
            StopReason.END_TURN, StopReason.UNKNOWN,
        } and (plain.usage.input_tokens or 0) > 0 and (plain.usage.output_tokens or 0) > 0
        probes.append({
            "name": "streaming_plain_message_stop_usage", "passed": plain_passed,
            "details": {
                "stop_reason": plain.stop_reason.value,
                "usage": plain.usage.as_dict(), "item_count": len(plain.items),
            },
        })
        probes.append({
            "name": "zero_payload_retry", "passed": zero_payload_proxy.calls == 2,
            "details": {"physical_attempts": zero_payload_proxy.calls, "logical_turns": 1},
        })

        tool_parameters = _forced_tool_parameters(base_parameters, {
            "type": "function", "function": {"name": PROBE_TOOL["name"]},
        })
        requested = await self.adapter.invoke(ProviderRequest(
            messages=({
                "role": "user",
                "content": "Call myclaw_conformance_echo exactly once with value native-v2.",
            },), tools=(PROBE_TOOL,), parameters=tool_parameters,
        ))
        tool_passed = (
            len(requested.tool_calls) == 1
            and requested.tool_calls[0].name == PROBE_TOOL["name"]
            and requested.tool_calls[0].arguments.get("value") == "native-v2"
            and requested.stop_reason == StopReason.TOOL_USE
        )
        probes.append({
            "name": "native_tool_call", "passed": tool_passed,
            "details": {
                "result": requested.as_dict(),
                "runtime_extra_body": base_parameters.get("extra_body"),
                "forced_choice_extra_body": tool_parameters.get("extra_body"),
            },
        })
        if not tool_passed:
            return ConformanceReport(False, tuple(probes))

        call = requested.tool_calls[0]
        continued = await self.adapter.invoke(ProviderRequest(
            messages=(
                {
                    "role": "user",
                    "content": (
                        "Call the echo Tool exactly once and then report its result. "
                        "After the Tool result below, do not call any Tool again; return only "
                        "an ordinary Assistant Message describing that completed result."
                    ),
                },
                {
                    "role": "assistant", "content": "",
                    "tool_calls": [{
                        "id": call.call_id, "type": "tool_use", "name": call.name,
                        "input": call.arguments,
                    }],
                },
                {
                    "role": "tool", "tool_use_id": call.call_id,
                    "content": json.dumps({"status": "succeeded", "data": {"value": "native-v2"}}),
                },
            ),
            # The Tool stays visible after use; the Provider must still be able
            # to produce an ordinary final message without consuming its type.
            tools=(PROBE_TOOL,), parameters=base_parameters,
        ))
        continuation_passed = bool(continued.assistant_messages) and not continued.tool_calls
        probes.append({
            "name": "tool_result_to_plain_final_tool_still_visible",
            "passed": continuation_passed,
            "details": continued.as_dict(),
        })

        repeated = await self.adapter.invoke(ProviderRequest(
            messages=({
                "role": "user",
                "content": "A new business request: call myclaw_conformance_echo with value native-v2-repeat.",
            },), tools=(PROBE_TOOL,), parameters=tool_parameters,
        ))
        repeat_call = repeated.tool_calls[0] if len(repeated.tool_calls) == 1 else None
        repeated_passed = bool(
            repeat_call and repeat_call.name == PROBE_TOOL["name"]
            and repeat_call.arguments.get("value") == "native-v2-repeat"
        )
        if repeat_call:
            repeat_final = await self.adapter.invoke(ProviderRequest(
                messages=(
                    {
                        "role": "user",
                        "content": (
                            "Call the echo Tool exactly once and then report its result. "
                            "After the Tool result below, do not call any Tool again; return only "
                            "an ordinary Assistant Message describing that completed result."
                        ),
                    },
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": repeat_call.call_id, "type": "tool_use", "name": repeat_call.name,
                        "input": repeat_call.arguments,
                    }]},
                    {"role": "tool", "tool_use_id": repeat_call.call_id, "content": json.dumps({
                        "status": "succeeded", "data": {"value": "native-v2-repeat"},
                    })},
                ), tools=(PROBE_TOOL,), parameters=base_parameters,
            ))
            repeated_passed = repeated_passed and bool(
                repeat_final.assistant_messages and not repeat_final.tool_calls
            )
        probes.append({
            "name": "repeated_same_tool_multi_round", "passed": repeated_passed,
            "details": {
                "first_call_id": call.call_id,
                "second_call_id": repeat_call.call_id if repeat_call else None,
                "tool_still_visible": True,
                "tool_call_result": repeated.as_dict(),
                "final_result": repeat_final.as_dict() if repeat_call else None,
            },
        })

        repair = await self.adapter.invoke(ProviderRequest(
            messages=(
                {"role": "user", "content": "Call the echo Tool; your previous arguments were invalid."},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "invalid-call", "type": "tool_use", "name": PROBE_TOOL["name"],
                    "input": {},
                }]},
                {"role": "tool", "tool_use_id": "invalid-call", "content": json.dumps({
                    "status": "failed", "error": {
                        "code": "tool_arguments_schema_error",
                        "message": "$.value is required", "category": "validation",
                    },
                })},
            ), tools=(PROBE_TOOL,), parameters=tool_parameters,
        ))
        probes.append({
            "name": "bad_arguments_schema_feedback_repair",
            "passed": bool(
                len(repair.tool_calls) == 1
                and repair.tool_calls[0].name == PROBE_TOOL["name"]
                and isinstance(repair.tool_calls[0].arguments.get("value"), str)
            ),
            "details": repair.as_dict(),
        })

        typed_repair = await self.adapter.invoke(ProviderRequest(
            messages=(
                {
                    "role": "user",
                    "content": (
                        "Call myclaw_conformance_typed_repair exactly once with "
                        "occurrences equal to the integer 10."
                    ),
                },
                {
                    "role": "assistant", "content": "",
                    "tool_calls": [{
                        "id": "invalid-typed-call", "type": "tool_use",
                        "name": TYPED_REPAIR_PROBE_TOOL["name"],
                        "input": {"occurrences": "10"},
                    }],
                },
                {
                    "role": "tool", "tool_use_id": "invalid-typed-call",
                    "content": json.dumps({
                        "status": "failed",
                        "summary": "Tool arguments failed schema validation",
                        "error": {
                            "code": "tool_arguments_schema_error",
                            "message": (
                                "/occurrences must be a native JSON integer. "
                                "Use 10 rather than the string \"10\" and retry the same Tool."
                            ),
                            "category": "validation",
                        },
                    }),
                },
            ),
            # Deliberately retain the normal runtime parameters instead of
            # forcing Tool choice or disabling provider thinking. This probe
            # covers the same repair path used after a real schema rejection.
            tools=(TYPED_REPAIR_PROBE_TOOL,), parameters=base_parameters,
        ))
        typed_call = (
            typed_repair.tool_calls[0]
            if len(typed_repair.tool_calls) == 1 else None
        )
        typed_repair_passed = bool(
            typed_call
            and typed_call.name == TYPED_REPAIR_PROBE_TOOL["name"]
            and typed_call.arguments.get("occurrences") == 10
            and not isinstance(typed_call.arguments.get("occurrences"), bool)
            and typed_repair.stop_reason == StopReason.TOOL_USE
        )
        probes.append({
            "name": "runtime_native_json_type_repair",
            "passed": typed_repair_passed,
            "details": typed_repair.as_dict(),
        })

        parallel_declared = capabilities.get("parallel_tool_calling") is True
        parallel_result = None
        parallel_passed = True
        if parallel_declared:
            parallel_parameters = _forced_tool_parameters(base_parameters, "required")
            parallel_result = await self.adapter.invoke(ProviderRequest(
                messages=({
                    "role": "user",
                    "content": (
                        "In one response call both echo Tools, the first with value one "
                        "and the second with value two."
                    ),
                },), tools=(PROBE_TOOL, SECOND_PROBE_TOOL), parameters=parallel_parameters,
            ))
            parallel_passed = (
                len(parallel_result.tool_calls) == 2
                and {item.name for item in parallel_result.tool_calls} == {
                    PROBE_TOOL["name"], SECOND_PROBE_TOOL["name"],
                }
                and len({item.call_id for item in parallel_result.tool_calls}) == 2
            )
        probes.append({
            "name": "parallel_tool_calls",
            "passed": parallel_passed,
            "details": {
                "declared": parallel_declared,
                "status": "probed" if parallel_declared else "not_declared",
                "result": parallel_result.as_dict() if parallel_result else None,
            },
        })

        fault = _PayloadThenError()
        payload_replay_fenced = False
        try:
            await ModelAdapter(fault).invoke(ProviderRequest(
                messages=({"role": "user", "content": "fault probe"},),
                tools=(), parameters=base_parameters,
            ))
        except ProviderError as error:
            payload_replay_fenced = error.received_payload and fault.calls == 1
        probes.append({
            "name": "payload_then_error_no_replay", "passed": payload_replay_fenced,
            "details": {"physical_attempts": fault.calls},
        })

        pending = [{
            "role": "tool", "tool_use_id": "long-context-call",
            "content": json.dumps({"status": "succeeded", "data": {"value": "authoritative"}}),
        }]
        compacted = ContextAssembler().assemble(
            platform_rules="conformance", agent_instructions="preserve facts",
            current_input="continue after compaction",
            history=[{"role": "user", "content": "历史 " * 20_000}],
            pending_results=pending, token_budget=1024,
        )
        probes.append({
            "name": "long_context_compaction",
            "passed": bool(
                compacted.compaction
                and any(
                    item.get("role") == "user"
                    and item.get("content") == "continue after compaction"
                    for item in compacted.messages
                )
                and any(item.get("tool_use_id") == "long-context-call" for item in compacted.messages)
            ),
            "details": compacted.compaction or {},
        })
        probes.append({
            "name": "tool_result_correlation",
            "passed": bool(call.call_id and repeat_call and repeat_call.call_id != call.call_id),
            "details": {"call_ids": [call.call_id, repeat_call.call_id if repeat_call else None]},
        })
        coordination_cases = (
            (
                "update_plan", {"items": [{"text": "Verify provider schema", "status": "completed"}]},
                "Call update_plan exactly once with one completed item whose text is Verify provider schema.",
            ),
            (
                "ask_user", {"question": "Which region should be used?"},
                "Call ask_user exactly once with question: Which region should be used?",
            ),
            (
                "invoke_agent",
                {"agent_id": "agent-conformance", "task": "Review the evidence."},
                (
                    "Call invoke_agent exactly once with agent_id agent-conformance "
                    "and task Review the evidence."
                ),
            ),
        )
        coordination_details = []
        coordination_passed = True
        for tool_name, expected_arguments, prompt in coordination_cases:
            coordination_parameters = _forced_tool_parameters(base_parameters, {
                "type": "function", "function": {"name": tool_name},
            })
            coordination_result = await self.adapter.invoke(ProviderRequest(
                messages=({"role": "user", "content": prompt},),
                tools=COORDINATION_PROBE_TOOLS,
                parameters=coordination_parameters,
            ))
            observed = (
                coordination_result.tool_calls[0]
                if len(coordination_result.tool_calls) == 1 else None
            )
            case_passed = bool(
                observed
                and observed.name == tool_name
                and observed.arguments == expected_arguments
                and coordination_result.stop_reason == StopReason.TOOL_USE
            )
            coordination_passed = coordination_passed and case_passed
            coordination_details.append({
                "tool_name": tool_name,
                "passed": case_passed,
                "expected_arguments": expected_arguments,
                "result": coordination_result.as_dict(),
            })
        probes.append({
            "name": "runtime_coordination_tool_schemas",
            "passed": coordination_passed,
            "details": {
                "schema_bundle_hash": _hash(COORDINATION_PROBE_TOOLS),
                "sent_tool_names": [item["name"] for item in COORDINATION_PROBE_TOOLS],
                "runtime_extra_body": base_parameters.get("extra_body"),
                "forced_choice_extra_body": _forced_tool_parameters(
                    base_parameters, "required",
                ).get("extra_body"),
                "cases": coordination_details,
            },
        })
        return ConformanceReport(all(item["passed"] for item in probes), tuple(probes))


async def execute_conformance_run(
    db: AsyncSession,
    *,
    version: ModelVersion,
    idempotency_key: str,
    provider: BaseLLM | None = None,
    embedding_client: OpenAICompatibleEmbeddingClient | None = None,
) -> ConformanceRun:
    suite_version = (
        settings.embedding_conformance_suite_version
        if is_embedding_model(version)
        else settings.provider_conformance_suite_version
    )
    request = {
        "model_version_id": version.id,
        "suite_version": suite_version,
    }
    run = ConformanceRun(
        id=uuid7(), subject_type="model_version", subject_id=version.id,
        suite_version=suite_version,
        idempotency_key=idempotency_key, request_hash=_hash(request),
        status="running", started_at=utcnow(),
    )
    db.add(run)
    await db.flush()
    try:
        if is_embedding_model(version):
            spec = embedding_spec(version)
            owns_embedding_client = embedding_client is None
            client = embedding_client or OpenAICompatibleEmbeddingClient(
                timeout_seconds=settings.knowledge_embedding_timeout_seconds,
            )
            try:
                batch = await client.embed(
                    spec=spec,
                    api_key=await resolve_provider_credential(db, version.credential_ref),
                    texts=["MyClaw semantic probe", "MyClaw semantic probe"],
                )
            finally:
                if owns_embedding_client:
                    await client.close()
            left, right = batch.vectors
            denominator = (
                sum(value * value for value in left) ** 0.5
                * sum(value * value for value in right) ** 0.5
            )
            similarity = sum(a * b for a, b in zip(left, right)) / denominator
            passed = similarity >= 0.999
            report = ConformanceReport(passed=passed, probes=({
                "name": "embedding_shape_determinism",
                "passed": passed,
                "details": {
                    "dimensions": len(left),
                    "declared_dimensions": spec.dimensions,
                    "duplicate_similarity": round(similarity, 8),
                    "input_tokens": batch.input_tokens,
                },
            },))
        else:
            if provider is None:
                provider = ModelHub.get(
                    {}, version.provider,
                    await resolve_provider_credential(db, version.credential_ref),
                    version.endpoint,
                )
            report = await ProviderConformanceSuite(provider).run(version)
        run.status = "passed" if report.passed else "failed"
        run.result_summary = report.as_dict()
    except Exception as error:
        run.status = "failed"
        run.result_summary = {
            "passed": False,
            "probes": [{
                "name": "suite_execution", "passed": False,
                "details": {
                    "error_code": getattr(error, "code", type(error).__name__),
                    "retryable": bool(getattr(error, "retryable", False)),
                    "received_payload": bool(
                        getattr(error, "received_payload", False)
                    ),
                },
            }],
        }
    run.ended_at = utcnow()
    run.content_hash = _hash({
        "request": request, "status": run.status, "result_summary": run.result_summary,
    })
    await db.flush()
    return run
