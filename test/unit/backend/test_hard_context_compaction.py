from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace

import pytest

from app.runtime.context_assembler import ContextPackage, request_manifest
from app.runtime import hard_compaction as subject
from app.runtime.hard_compaction import HardCompactionError, apply_hard_compactions, compact_context
from app.runtime.protocol import AssistantMessageItem, ContentPart, NormalizedTurnResult, StopReason, ToolCallItem, Usage
from app.runtime.token_counting import conservative_request_tokens


pytestmark = pytest.mark.unit
PARAMETERS = {"model_id": "frozen-model", "max_tokens": 1000}


def result(text="The task marker is ORBIT. Keep the latest constraints and pending work.", **kwargs):
    return NormalizedTurnResult(
        items=(AssistantMessageItem((ContentPart("text", text),)),),
        stop_reason=kwargs.pop("stop_reason", StopReason.END_TURN),
        usage=Usage(input_tokens=41, output_tokens=17), **kwargs,
    )


def package(*, history=None, pending=(), current="Continue the task without changing its constraints.", hard=2400):
    history = history if history is not None else [
        {"role": "user", "content": "TASK ORBIT " + "old evidence " * 350},
        {"role": "assistant", "content": "Verified old result " * 200},
    ]
    raw = [{"role": "system", "content": "Platform rules stay intact."}, *history,
           {"role": "user", "content": current}, *pending]
    sources = [{"source": "platform+agent+permission"},
               *[{"source": "history", "source_message_id": f"message-{index}"}
                 for index in range(len(history))],
               {"source": "current_input"},
               *[{"source": "continuation"} for _ in pending]]
    manifests = tuple(request_manifest(raw, sources))
    return ContextPackage(
        messages=tuple(raw), manifest=manifests, source_messages=tuple(copy.deepcopy(raw)),
        source_manifest=copy.deepcopy(manifests), estimated_tokens=conservative_request_tokens(raw),
        history_start=1, history_count=len(history), recent_history_messages=6,
        soft_input_target=int(hard * 0.8), hard_input_limit=hard,
    )


class Invoker:
    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer or result()

    async def __call__(self, request, metadata):
        self.calls.append((copy.deepcopy(request), copy.deepcopy(metadata)))
        return self.answer


async def compact(value, invoke, **kwargs):
    return await compact_context(value, provider=object(), provider_type="openai_compatible",
                                 tools=[], parameters=PARAMETERS, invoke_summary=invoke, **kwargs)


@pytest.mark.asyncio
async def test_hard_summary_is_atomic_view_with_exact_sources_and_bounded_requests():
    original = package(hard=2000)
    saved = copy.deepcopy(original)
    invoke = Invoker()
    reduced = await compact(original, invoke)
    assert original == saved
    assert reduced.source_messages == saved.source_messages
    assert reduced.source_manifest == saved.source_manifest
    assert (reduced.history_start, reduced.history_count) == (1, 2)
    assert reduced.messages[0] == original.messages[0]
    assert reduced.messages[-1] == original.messages[-1]
    assert reduced.messages[1]["role"] == "assistant"
    assert "ORBIT" in reduced.messages[1]["content"]
    assert reduced.pre_call_tokens <= original.hard_input_limit < original.estimated_tokens
    cache = reduced.compaction["hard_compactions"][0]
    assert cache["trigger"] == "hard_input_limit"
    assert cache["source_checksums"] == [section["checksum"] for section in original.source_manifest[1:3]]
    assert [section["source_message_id"] for section in cache["source_sections"]] == ["message-0", "message-1"]
    assert cache["summary_hash"] == subject._text_hash(cache["summary"])
    assert cache["before_tokens"] > cache["after_tokens"]
    assert cache["summary_calls"] == len(invoke.calls)
    assert cache["usage"]["input_tokens"] == 41 * len(invoke.calls)
    fragments = []
    end = 0
    for request, metadata in invoke.calls:
        fragment = json.loads(request.messages[1]["content"])
        assert request.tools == ()
        assert metadata["source_hash"] == cache["source_hash"]
        assert fragment["start_char"] == end
        end = fragment["end_char"]
        fragments.append(fragment["source_fragment"])
        assert conservative_request_tokens(request.messages, request.tools, request.parameters) <= metadata["summary_input_limit"]
        assert metadata["pre_call_count"] + metadata["max_output_tokens"] < original.hard_input_limit
    decoded = json.loads("".join(fragments))
    assert [item["message"] for item in decoded] == list(original.source_messages[1:3])


@pytest.mark.asyncio
async def test_valid_cache_replays_without_another_model_call_and_rejects_mutation():
    original = package(hard=2000)
    reduced = await compact(original, Invoker())
    replay_input = replace(original, compaction=copy.deepcopy(reduced.compaction))
    replay = apply_hard_compactions(replay_input)
    assert replay.messages == reduced.messages
    invoke = Invoker()
    fit = await compact(replay_input, invoke)
    assert not invoke.calls and fit.messages == reduced.messages
    for corruption in ("source", "summary", "identity"):
        changed = copy.deepcopy(replay_input)
        if corruption == "source":
            changed.source_messages[1]["content"] += " changed"
        elif corruption == "summary":
            changed.compaction["hard_compactions"][0]["summary"] += " changed"
        else:
            changed.source_manifest[1]["source_message_id"] = "another-message"
        invalid = apply_hard_compactions(changed)
        assert not any(section["source"] == "context_compaction" for section in invalid.manifest)
        assert len(invalid.messages) == len(original.messages)


@pytest.mark.asyncio
async def test_extended_prefix_reuses_validated_prior_summary_instead_of_old_raw():
    original = package(hard=2000)
    reduced = await compact(original, Invoker())
    raw = list(original.source_messages)
    raw.insert(3, {"role": "user", "content": "NEW-DELTA " * 900})
    sources = list(original.source_manifest)
    sources.insert(3, {"source": "history", "source_message_id": "new-message"})
    manifests = tuple(request_manifest(raw, sources))
    extended = replace(original, messages=tuple(raw), source_messages=tuple(raw), manifest=manifests,
                       source_manifest=manifests, history_count=3, compaction=copy.deepcopy(reduced.compaction))
    invoke = Invoker(result("ORBIT and the new delta are retained."))
    final = await compact(extended, invoke)
    text = "".join(json.loads(request.messages[1]["content"])["source_fragment"] for request, _ in invoke.calls)
    assert "validated_prior_summary" in text
    assert reduced.compaction["hard_compactions"][0]["summary"] in text
    assert "NEW-DELTA" in text
    assert "old evidence old evidence" not in text
    assert len(final.compaction["hard_compactions"]) == 1
    assert len(final.compaction["hard_compactions"][0]["source_sections"]) == 3
    assert final.source_messages == extended.source_messages


def batch(call_id, content, *, name="read_file", status="succeeded"):
    return [{"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "name": name, "input": {"path": "evidence.txt"}},
    ]}, {"role": "tool", "tool_call_id": call_id,
         "content": json.dumps({"status": status, "data": {"content": content}})}]


@pytest.mark.asyncio
async def test_consumed_tool_prefix_is_atomic_and_newest_pending_batch_is_protected():
    consumed = batch("old-call", "verified long tool source " * 250)
    latest = batch("new-call", "UNCONSUMED-RESULT")
    original = package(history=[], pending=[*consumed, *latest], hard=2000)
    invoke = Invoker()
    reduced = await compact(original, invoke)
    assert reduced.source_messages == original.source_messages
    assert list(reduced.messages[-2:]) == latest
    assert reduced.messages[1] == original.messages[1]
    covered = reduced.compaction["hard_compactions"][0]["source_sections"]
    assert covered[0]["tool_call_ids"] == ["old-call"]
    assert covered[1]["tool_call_id"] == "old-call"
    assert "new-call" not in json.dumps(covered)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing_result", "failure", "state_tool"])
async def test_incomplete_failed_or_state_tool_prefix_cannot_be_summarized(mode):
    pending = batch("first", "protected source " * 400,
                    status="failed" if mode == "failure" else "succeeded",
                    name="update_plan" if mode == "state_tool" else "read_file")
    if mode == "missing_result":
        pending[0]["tool_calls"].append({"id": "missing", "name": "read_file", "input": {}})
    original = package(history=[], pending=[*pending, *batch("last", "pending")], hard=1200)
    invoke = Invoker()
    with pytest.raises(HardCompactionError) as raised:
        await compact(original, invoke)
    assert raised.value.code == "hard_compaction_no_source"
    assert not invoke.calls


@pytest.mark.asyncio
async def test_protected_base_and_tool_schemas_are_checked_before_provider():
    original = package(current="current request " * 400, hard=1500)
    invoke = Invoker()
    with pytest.raises(HardCompactionError) as raised:
        await compact(original, invoke)
    assert raised.value.code == "hard_compaction_protected_overflow"
    assert not invoke.calls
    with pytest.raises(HardCompactionError) as raised:
        await compact_context(package(hard=1500), provider=object(), provider_type="openai_compatible",
                              tools=[{"name": "large-schema", "description": "x" * 6000}],
                              parameters=PARAMETERS, invoke_summary=invoke)
    assert raised.value.code == "hard_compaction_protected_overflow"
    assert not invoke.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [StopReason.MAX_TOKENS, StopReason.CONTENT_FILTER, StopReason.UNKNOWN, StopReason.ERROR])
async def test_invalid_summary_never_installs_partial_cache(stop):
    original = package(hard=2000)
    saved = copy.deepcopy(original)
    with pytest.raises(HardCompactionError) as raised:
        await compact(original, Invoker(result(stop_reason=stop)))
    assert raised.value.code == "hard_compaction_output_invalid"
    assert raised.value.usage["input_tokens"] == 41
    assert original == saved


@pytest.mark.asyncio
async def test_summary_tool_calls_are_rejected_even_with_end_turn():
    answer = NormalizedTurnResult(items=(ToolCallItem("bad", "write_file", {}, 0),), stop_reason=StopReason.END_TURN)
    with pytest.raises(HardCompactionError) as raised:
        await compact(package(hard=2000), Invoker(answer))
    assert raised.value.code == "hard_compaction_output_invalid"


@pytest.mark.asyncio
async def test_source_and_call_limits_are_bounded_and_failure_preserves_input(monkeypatch):
    original = package(hard=2000)
    saved = copy.deepcopy(original)
    monkeypatch.setattr(subject, "MAX_SOURCE_BYTES", 100)
    invoke = Invoker()
    with pytest.raises(HardCompactionError) as raised:
        await compact(original, invoke)
    assert raised.value.code == "hard_compaction_source_limit" and not invoke.calls
    monkeypatch.setattr(subject, "MAX_SOURCE_BYTES", 192000)
    monkeypatch.setattr(subject, "MAX_SUMMARY_CALLS", 1)
    with pytest.raises(HardCompactionError) as raised:
        await compact(original, invoke)
    assert raised.value.code == "hard_compaction_call_limit" and len(invoke.calls) == 1
    assert original == saved


@pytest.mark.asyncio
async def test_source_mutation_during_call_cannot_be_promoted_to_valid_cache():
    original = package(hard=2000)

    async def mutate(request, metadata):
        original.source_messages[1]["content"] = "changed concurrently"
        return result()

    with pytest.raises(HardCompactionError) as raised:
        await compact(original, mutate)
    assert raised.value.code == "hard_compaction_source_changed"
    assert not original.compaction


@pytest.mark.asyncio
async def test_outer_timeout_and_external_cancellation_are_distinct(monkeypatch):
    monkeypatch.setattr(subject, "COMPACTION_TIMEOUT_SECONDS", 0.01)

    async def slow(request, metadata):
        await asyncio.sleep(5)

    with pytest.raises(HardCompactionError) as raised:
        await compact(package(hard=2000), slow)
    assert raised.value.code == "hard_compaction_timeout"

    async def cancelled(request, metadata):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await compact(package(hard=2000), cancelled)


@pytest.mark.asyncio
async def test_force_recovery_requires_strict_reduction_and_cleans_summary_parameters():
    original = package(history=[{"role": "user", "content": "source fact " * 30}], hard=4000)
    invoke = Invoker(result("short verified summary"))
    reduced = await compact_context(original, provider=object(), provider_type="openai_compatible", tools=[],
                                    parameters={**PARAMETERS, "tool_choice": "required", "response_format": {"type": "json_object"}},
                                    invoke_summary=invoke, force=True)
    assert reduced.compaction["hard_compactions"][0]["trigger"] == "provider_context_length"
    assert "tool_choice" not in invoke.calls[0][0].parameters
    assert "response_format" not in invoke.calls[0][0].parameters
    with pytest.raises(HardCompactionError) as raised:
        await compact(original, Invoker(result("longer summary " * 80)), force=True)
    assert raised.value.code == "hard_compaction_not_reduced"


@pytest.mark.asyncio
async def test_native_counter_validates_each_summary_and_complete_final_request():
    class Provider:
        def __init__(self):
            self.counts = []

        async def count_tokens(self, messages, tools, parameters):
            total = conservative_request_tokens(messages, tools, parameters) + 300
            self.counts.append(total)
            return total

    provider = Provider()
    invoke = Invoker()
    reduced = await compact_context(package(hard=2400), provider=provider, provider_type="anthropic",
                                    tools=[], parameters=PARAMETERS, invoke_summary=invoke)
    assert reduced.count_method == "anthropic_native"
    assert reduced.pre_call_tokens == provider.counts[-1] <= 2400
    assert all(metadata["pre_call_count"] <= metadata["summary_input_limit"] for _, metadata in invoke.calls)


@pytest.mark.asyncio
async def test_default_invoker_uses_model_adapter_without_transparent_retry(monkeypatch):
    calls = []

    class Adapter:
        def __init__(self, provider, *, max_zero_payload_retries):
            assert max_zero_payload_retries == 0

        async def invoke(self, request):
            calls.append(request)
            return result()

    monkeypatch.setattr(subject, "ModelAdapter", Adapter)
    reduced = await compact_context(package(hard=2000), provider=object(), provider_type="openai_compatible",
                                    tools=[], parameters=PARAMETERS)
    assert calls and reduced.pre_call_tokens <= 2000


@pytest.mark.asyncio
async def test_latest_overlapping_cache_wins_once_and_single_message_cache_cannot_bypass_validation():
    original = package(history=[{"role": "user", "content": "Canonical record " * 400}], hard=2000)
    reduced = await compact(original, Invoker(result("Older validated state")))
    newer = copy.deepcopy(reduced.compaction["hard_compactions"][0])
    newer["summary"] = "Newer validated state"
    newer["summary_hash"] = subject._text_hash(newer["summary"])
    newer["id"] = subject._hash([newer["source_hash"], newer["summary_hash"]])
    merged = replace(reduced, compaction={"hard_compactions": [*reduced.compaction["hard_compactions"], newer]})
    replayed = apply_hard_compactions(merged)
    assert len(replayed.messages) == len(original.messages)
    assert "Newer validated state" in replayed.messages[1]["content"]
    assert "Older validated state" not in str(replayed.messages)
    assert sum(section["source"] == "context_compaction" for section in replayed.manifest) == 1
    replayed.source_messages[1]["content"] = "Changed original"
    invalidated = apply_hard_compactions(replayed)
    assert invalidated.messages[1]["content"] == "Changed original"
    assert not any(section["source"] == "context_compaction" for section in invalidated.manifest)


@pytest.mark.asyncio
async def test_unicode_source_slices_are_lossless_and_failed_final_count_is_not_committed():
    original = package(history=[{"role": "user", "content": "完整原始证据" * 400}], hard=2400)
    invoke = Invoker()
    reduced = await compact(original, invoke)
    joined = "".join(json.loads(request.messages[1]["content"])["source_fragment"] for request, _ in invoke.calls)
    assert json.loads(joined)[0]["message"] == original.source_messages[1]
    assert reduced.source_messages == original.source_messages

    over = package(history=[{"role": "user", "content": "small original source"}],
                   current="Protected current constraint " * 110, hard=2000)
    saved = copy.deepcopy(over)
    # A text can satisfy the byte ceiling and still exceed the complete
    # request budget: the final real recount must reject it atomically.
    with pytest.raises(HardCompactionError) as raised:
        await compact(over, Invoker(result("s" * 3000)), force=True)
    assert raised.value.code == "hard_compaction_insufficient"
    assert over == saved


@pytest.mark.asyncio
async def test_parallel_tool_results_stay_with_all_original_calls():
    first = batch("a", "source A " * 200)
    first[0]["tool_calls"].append({"id": "b", "name": "read_file", "input": {"path": "b.txt"}})
    first += batch("b", "source B " * 200)[1:]
    latest = batch("last", "pending")
    original = package(history=[], pending=[*first, *latest], hard=1400)
    invoke = Invoker()
    reduced = await compact(original, invoke)
    assert reduced.source_messages == original.source_messages
    assert list(reduced.messages[-2:]) == latest
    cache = reduced.compaction["hard_compactions"][0]
    assert len(cache["source_sections"]) == 3
    assert cache["source_sections"][0]["tool_call_ids"] == ["a", "b"]
    assert {section.get("tool_call_id") for section in cache["source_sections"][1:]} == {"a", "b"}
