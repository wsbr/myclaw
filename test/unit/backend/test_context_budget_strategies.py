"""Scenario checks for the three profiles, complete budgets and source retention."""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from app.core.model_hub.base import BaseLLM, LLMChunk, TokenUsage, ToolCall
from app.core.tool_registry.base import RuntimeContext, ToolResult
from app.runtime.action_dispatcher import DispatchResult
from app.runtime.agent_loop_coordinator import AgentLoopCoordinator, AgentLoopError, LoopIdentity
from app.runtime.context_assembler import ContextAssembler, ContextPackage, request_manifest
from app.runtime.context_budget import fit_context_budget, project_consumed_tool_results
from app.runtime.model_adapter import ModelAdapter
from app.runtime.protocol import ProviderError
from app.runtime.token_counting import conservative_request_tokens
from app.services.context_policy_service import resolve_context_parameters

pytestmark = pytest.mark.unit


class CountingProvider(BaseLLM):
    async def chat(self, messages, tools, params):
        raise NotImplementedError

    async def stream(self, messages, tools, params):
        raise NotImplementedError
        yield

    async def count_tokens(self, messages, tools=None, params=None):
        return conservative_request_tokens(messages, tools, params)


class SummarizingProvider(CountingProvider):
    async def stream(self, messages, tools, params):
        assert not tools
        yield LLMChunk(type="text_delta", content="Earlier background retained; work remains pending.")
        yield LLMChunk(type="done", stop_reason="stop", usage=TokenUsage(input_tokens=100, output_tokens=10))


def tool_round(number, *, name="execute", failed=False):
    value = ToolResult(
        success=not failed, summary=f"Result {number}",
        data={"stdout": f"START-{number}-" + "资料原文" * 700 + f"-END-{number}", "exit_code": 0},
        evidence=[{"path": f"report-{number}.txt", "checksum": "source-checksum"}],
        artifact_ids=[f"artifact-{number}"],
    )
    return [
        {"role": "assistant", "content": f"Decision {number}", "tool_calls": [
            {"id": f"call-{number}", "name": name, "input": {"command": "inspect"}},
        ]},
        {"role": "tool", "tool_call_id": f"call-{number}", "content": json.dumps(value.model_dump(mode="json"), ensure_ascii=False)},
    ]


def test_history_parallel_tool_batch_is_atomic_and_incomplete_batch_is_protected():
    calls = [{"id": "a", "name": "read"}, {"id": "b", "name": "read"}]
    history = [{"role": "assistant", "content": "", "tool_calls": calls},
               {"role": "tool", "tool_call_id": "b", "content": "b-result"},
               {"role": "tool", "tool_call_id": "a", "content": "a-result"},
               {"role": "assistant", "content": "retained"}]
    assembler = ContextAssembler()
    package = assembler.assemble(platform_rules="rules", agent_instructions="", current_input="goal",
                                 history=history, token_budget=10000)
    trimmed = assembler.drop_oldest_history(package, reason="test")
    assert trimmed.history_count == 1
    assert len(trimmed.compaction["omitted_sections"]) == 3
    assert trimmed.messages[-2]["content"] == "retained"
    incomplete = replace(package, history_count=2)
    assert assembler.drop_oldest_history(incomplete, reason="test") == incomplete
    assert package.messages[1]["tool_calls"] == calls


@pytest.mark.asyncio
async def test_hard_limit_summarizes_recent_history_without_deleting_canonical_records():
    package = ContextAssembler().assemble(
        platform_rules="rules", agent_instructions="", current_input="CURRENT GOAL",
        history=[{"role": "user", "content": "old" * 1000},
                 {"role": "assistant", "content": "recent" * 1000}],
        recent_history_messages=2, soft_input_target=100, hard_input_limit=10000,
        defer_history_trimming=True,
    )
    result = await fit_context_budget(package, provider=CountingProvider(), provider_type="openai", tools=[], parameters={})
    assert result.context.history_count == 2
    assert result.context.compaction["soft_target_unmet"]
    hard = await fit_context_budget(replace(package, hard_input_limit=2200), provider=SummarizingProvider(), provider_type="openai", tools=[], parameters={"max_tokens":512})
    assert hard.count.tokens <= 2200
    assert hard.context.history_count == 2
    assert hard.context.source_messages == package.messages
    assert hard.context.messages[-1] == {"role": "user", "content": "CURRENT GOAL"}
    assert hard.context.compaction["hard_compactions"]


@pytest.mark.asyncio
async def test_large_history_fits_with_logarithmic_native_count_calls():
    package = ContextAssembler().assemble(
        platform_rules="rules", agent_instructions="", current_input="current",
        history=[{"role": "assistant", "content": f"history-{index}:" + "text " * 200} for index in range(200)],
        soft_input_target=3000, hard_input_limit=100000, recent_history_messages=2,
        defer_history_trimming=True,
    )
    fitted = await fit_context_budget(package, provider=CountingProvider(), provider_type="openai", tools=[], parameters={})
    assert 2 <= fitted.context.history_count < 15
    assert fitted.count.tokens <= 3000
    assert fitted.count_calls <= 10
    assert fitted.context.source_messages[-1]["content"] == "current"


def test_consumed_cleanup_preserves_recent_batch_errors_state_evidence_and_literal_excerpts():
    messages = [*tool_round(1), *tool_round(2, failed=True), *tool_round(3, name="update_plan"), *tool_round(4)]
    before = deepcopy(messages)
    projected, stats = project_consumed_tool_results(messages, recent_tool_rounds=1, max_tokens=512)
    assert [item["tool_call_id"] for item in stats] == ["call-1"]
    result = json.loads(projected[1]["content"])
    original = json.loads(messages[1]["content"])
    assert result["status"] == "succeeded"
    assert result["artifact_ids"] == original["artifact_ids"]
    assert result["evidence"] == original["evidence"]
    assert result["data"]["source"]["exit_code"] == 0
    assert "START-1" in result["data"]["excerpt_prefix"]
    assert "END-1" in result["data"]["excerpt_suffix"]
    assert stats[0]["projected_tokens"] <= 512
    assert projected[2:] == messages[2:]
    assert projected[0] == messages[0]
    assert messages == before


@pytest.mark.asyncio
async def test_each_budget_pass_projects_from_canonical_results_and_has_stable_source_hashes():
    raw = [{"role": "system", "content": "rules"}, {"role": "user", "content": "goal"}, *tool_round(1), *tool_round(2)]
    package = ContextPackage(tuple(raw), tuple(request_manifest(raw)), 1,
                             soft_input_target=500, hard_input_limit=10000,
                             recent_tool_rounds=1, consumed_tool_result_tokens=512)
    first = await fit_context_budget(package, provider=CountingProvider(), provider_type="openai", tools=[], parameters={})
    second = await fit_context_budget(first.context, provider=CountingProvider(), provider_type="openai", tools=[], parameters={})
    assert first.tool_projection == second.tool_projection
    assert first.context.messages == second.context.messages
    assert first.context.source_messages == second.context.source_messages == tuple(raw)
    assert first.context.manifest[3]["source_checksum"] == package.manifest[3]["checksum"]
    assert first.context.manifest[3]["checksum"] != package.manifest[3]["checksum"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,protected_messages", [
    ("concise", 2), ("standard", 8), ("evidence", 4),
])
async def test_soft_pressure_keeps_the_scenario_conversation_window(name, protected_messages):
    profile = resolve_context_parameters(f"context.{name}")
    history = [{
        "role": "user" if index % 2 == 0 else "assistant",
        "content": f"Conversation {index}: " + "Earlier decision and follow-up. " * 20,
    } for index in range(12)]
    source_history = deepcopy(history)
    package = ContextAssembler().assemble(
        platform_rules="rules", agent_instructions="", current_input="Apply the latest correction",
        working_plan={"revision": 3, "goal": "Finish the current task"},
        history=history,
        history_manifest=[{"source_message_id": f"message-{index}"} for index in range(12)],
        context_policy_key=f"context.{name}",
        recent_history_messages=profile["recent_history_messages"],
        soft_input_target=100, hard_input_limit=100000, defer_history_trimming=True,
    )
    original_messages = deepcopy(package.messages)
    original_history_manifest = package.manifest[
        package.history_start:package.history_start + package.history_count
    ]
    fitted = await fit_context_budget(
        package, provider=CountingProvider(), provider_type="openai", tools=[], parameters={},
    )
    result = fitted.context
    assert result.history_count == protected_messages
    assert result.messages[result.history_start:result.history_start + result.history_count] == tuple(
        source_history[-protected_messages:]
    )
    assert result.messages[:package.history_start] == original_messages[:package.history_start]
    assert result.messages[-1] == {"role": "user", "content": "Apply the latest correction"}
    assert result.compaction["soft_target_unmet"]
    assert result.compaction["stages"][-1]["removed_messages"] == 12 - protected_messages
    assert not result.compaction.get("hard_compactions")
    assert [(item["source_message_id"], item["checksum"])
            for item in result.compaction["omitted_sections"]] == [
        (item["source_message_id"], item["checksum"])
        for item in original_history_manifest[:-protected_messages]
    ]
    assert history == source_history and package.messages == original_messages


@pytest.mark.asyncio
async def test_long_tasks_keep_more_tool_steps_while_evidence_keeps_richer_old_results():
    raw = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "Complete the task using the gathered sources"},
        *sum((tool_round(index) for index in range(1, 6)), []),
    ]
    original = deepcopy(raw)
    manifest = request_manifest(raw)
    results = {}
    for name, protected_batches in (("concise", 1), ("standard", 3), ("evidence", 2)):
        profile = resolve_context_parameters(f"context.{name}")
        package = ContextPackage(
            tuple(raw), tuple(manifest), 1, soft_input_target=500, hard_input_limit=100000,
            context_policy_key=f"context.{name}",
            recent_tool_rounds=profile["recent_tool_rounds"],
            consumed_tool_result_tokens=profile["consumed_tool_result_tokens"],
        )
        fitted = await fit_context_budget(
            package, provider=CountingProvider(), provider_type="openai", tools=[], parameters={},
        )
        results[name] = fitted.context
        projected_ids = {item["tool_call_id"] for item in fitted.tool_projection}
        assert projected_ids == {f"call-{index}" for index in range(1, 6 - protected_batches)}
        assert all(item["projected_tokens"] <= profile["consumed_tool_result_tokens"]
                   for item in fitted.tool_projection)
        assert fitted.context.source_messages == tuple(original)
        assert fitted.context.messages[:2] == tuple(original[:2])
        calls = [call["id"] for message in fitted.context.messages for call in message.get("tool_calls", [])]
        returned = [message["tool_call_id"] for message in fitted.context.messages if message["role"] == "tool"]
        assert calls == returned == [f"call-{index}" for index in range(1, 6)]
        for number in range(1, 6):
            call_index, result_index = 2 * number, 2 * number + 1
            assert fitted.context.messages[call_index] == original[call_index]
            if f"call-{number}" not in projected_ids:
                assert fitted.context.messages[result_index] == original[result_index]
                continue
            source = json.loads(original[result_index]["content"])
            view = json.loads(fitted.context.messages[result_index]["content"])
            assert view["status"] == source["status"]
            assert view["artifact_ids"] == source["artifact_ids"]
            assert view["evidence"] == source["evidence"]
            assert view["data"]["source"]["exit_code"] == 0
            assert f"START-{number}" in view["data"]["excerpt_prefix"]
            assert f"END-{number}" in view["data"]["excerpt_suffix"]
            assert fitted.context.manifest[result_index]["source_checksum"] == manifest[result_index]["checksum"]
    assert raw == original
    # Step 3 remains complete for execution continuity, but is projected when
    # preserving source detail across more evidence is the priority.
    assert results["standard"].messages[7] == original[7]
    assert results["evidence"].messages[7] != original[7]
    standard_old = json.loads(results["standard"].messages[3]["content"])["data"]
    evidence_old = json.loads(results["evidence"].messages[3]["content"])["data"]
    for excerpt in ("excerpt_prefix", "excerpt_suffix"):
        assert len(evidence_old[excerpt]) > len(standard_old[excerpt])


class LongTaskProvider(CountingProvider):
    def __init__(self):
        self.requests = []

    async def stream(self, messages, tools, params):
        self.requests.append(deepcopy(messages))
        turn = len(self.requests)
        if turn <= 6:
            yield LLMChunk(type="tool_call_delta", tool_call=ToolCall(id=f"call-{turn}", name="execute", arguments={}))
            reason = "tool_calls"
        else:
            yield LLMChunk(type="text_delta", content="All six steps completed")
            reason = "stop"
        yield LLMChunk(type="done", stop_reason=reason, model="test", usage=TokenUsage(input_tokens=10, output_tokens=1))


class LongTaskDispatcher:
    async def dispatch_batch(self, calls, **kwargs):
        result = ToolResult(success=True, summary="Step completed", data={"stdout": "raw-output " * 400, "exit_code": 0})
        return [DispatchResult(call.call_id, call.name, result, invocation_id="durable-" + call.call_id) for call in calls]


@pytest.mark.asyncio
async def test_long_tool_loop_continues_under_budget_and_retains_full_resume_results():
    provider = LongTaskProvider()
    context = ContextAssembler().assemble(
        platform_rules="rules", agent_instructions="", current_input="Complete six steps",
        working_plan={"revision": 1, "goal": "Complete six steps"},
        soft_input_target=2100, hard_input_limit=4400, recent_tool_rounds=1,
        consumed_tool_result_tokens=256,
    )
    result = await AgentLoopCoordinator(ModelAdapter(provider), LongTaskDispatcher()).run(
        context=context, tool_definitions=[], capability_snapshot={"id": "snapshot"},
        runtime_context=RuntimeContext(tenant_id="t", user_id="u", agent_id="a", session_id="s", runner_id="r"),
        model_parameters={}, identity=LoopIdentity("r", "w", "run", "a-v", "m-v", "openai"),
    )
    assert result.final_message == "All six steps completed"
    assert len(provider.requests) == 7
    assert all(conservative_request_tokens(request, []) <= 4400 for request in provider.requests)
    assert all(any(item.get("content") == "Complete six steps" for item in request) for request in provider.requests)
    assert all(any(str(item.get("content", "")).startswith("WorkingPlan:") for item in request) for request in provider.requests)
    tools = [item for item in result.messages if item["role"] == "tool"]
    assert len(tools) == 6
    assert all(json.loads(item["content"])["data"]["stdout"] == "raw-output " * 400 for item in tools)
    assert any("context_projection" in item.get("content", "") for item in provider.requests[-1])


class OverflowProvider(CountingProvider):
    def __init__(self, *, always=False, partial=False):
        self.requests, self.always, self.partial = [], always, partial

    async def stream(self, messages, tools, params):
        self.requests.append(deepcopy(messages))
        if self.always or len(self.requests) == 1:
            if self.partial:
                yield LLMChunk(type="text_delta", content="Partial reply")
            raise ProviderError("context_length_exceeded", code="provider_context_length")
        yield LLMChunk(type="text_delta", content="Recovered")
        yield LLMChunk(type="done", stop_reason="stop", model="test", usage=TokenUsage())


@pytest.mark.asyncio
@pytest.mark.parametrize("always,partial,history_count,expected_calls", [
    (False, False, 2, 3), (True, False, 2, 2), (False, True, 2, 1), (False, False, 0, 1),
])
async def test_provider_overflow_retries_once_only_after_safe_reduction(always, partial, history_count, expected_calls):
    provider = OverflowProvider(always=always, partial=partial)
    context = ContextAssembler().assemble(
        platform_rules="rules", agent_instructions="", current_input="CURRENT",
        history=[{"role": "assistant", "content": f"old-{index}:" + "background " * 120}
                 for index in range(history_count)],
        soft_input_target=2000, hard_input_limit=3000,
    )
    operation = AgentLoopCoordinator(ModelAdapter(provider), LongTaskDispatcher()).run(
        context=context, tool_definitions=[], capability_snapshot={"id": "snapshot"},
        runtime_context=RuntimeContext(tenant_id="t", user_id="u", agent_id="a", session_id="s", runner_id="r"),
        model_parameters={}, identity=LoopIdentity("r", "w", "run", "a-v", "m-v", "openai"),
    )
    if always or partial or not history_count:
        with pytest.raises(AgentLoopError):
            await operation
    else:
        assert (await operation).final_message == "Recovered"
    assert len(provider.requests) == expected_calls
    if expected_calls == 3:
        assert provider.requests[-1][-1]["content"] == "CURRENT"
        assert conservative_request_tokens(provider.requests[-1]) < conservative_request_tokens(provider.requests[0])
        assert "background " * 120 not in str(provider.requests[-1])
