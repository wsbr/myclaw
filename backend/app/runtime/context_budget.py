"""One request-budget path for initial assembly and every loop continuation.

All reductions are model views. Canonical messages remain available for durable
checkpoints; this module never rewrites a Message, ToolInvocation or Artifact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from app.core.tool_registry.base import ToolResult
from app.runtime.context_assembler import (
    ContextAssembler, ContextPackage, oldest_history_group_size, request_manifest,
)
from app.runtime.token_counting import RequestTokenCount, count_provider_request
from app.runtime.hard_compaction import apply_hard_compactions, compact_context
from app.runtime.web_result_projection import WEB_TOOL_NAMES, project_web_messages


_STATE_TOOLS = {"update_plan", "update_shared_task", "ask_user", "invoke_agent", "schedule_task"}
_LOCATORS = {"path", "url", "source_id", "checksum", "offset", "next_offset", "size",
             "truncated", "exit_code", "returncode", "artifact_id", "retrieved_at"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _tokens(value: Any) -> int:
    return (len(_json(value).encode("utf-8")) + 2) // 3


def project_consumed_tool_results(
    messages: list[dict], *, recent_tool_rounds: int, max_tokens: int,
) -> tuple[list[dict], list[dict]]:
    """Reduce only old, consumed successes; preserve calls, state and failures.

Excerpts are literal source slices, never a generated claim about the result.
Reference/evidence metadata is an irreducible floor, even if it exceeds budget.
"""

    projected = [dict(message) for message in messages]
    batches = [index for index, message in enumerate(messages)
               if message.get("role") == "assistant" and message.get("tool_calls")]
    protected = set(batches[-max(1, recent_tool_rounds):])
    calls = {}
    for index in batches:
        for call in messages[index]["tool_calls"]:
            calls[str(call.get("id"))] = (
                index, call.get("name") or (call.get("function") or {}).get("name"),
            )
    last_assistant = max((index for index, message in enumerate(messages)
                          if message.get("role") == "assistant"), default=-1)
    stats = []
    for index, message in enumerate(messages):
        call_id = str(message.get("tool_call_id") or message.get("tool_use_id") or "")
        batch, name = calls.get(call_id, (-1, None))
        if (message.get("role") != "tool" or batch < 0 or batch in protected
                or index >= last_assistant or name in WEB_TOOL_NAMES | _STATE_TOOLS):
            continue
        try:
            result = ToolResult.model_validate(json.loads(message.get("content") or "{}"))
        except (ValueError, TypeError):
            continue
        raw = result.model_dump(mode="json")
        if not result.success or _tokens(raw) <= max_tokens:
            continue
        digest = hashlib.sha256(_json(raw).encode()).hexdigest()
        locators = {key: value for key, value in (result.data or {}).items()
                    if key in _LOCATORS} if isinstance(result.data, dict) else {}
        text = _json({"data": result.data, "content_blocks": result.content_blocks})
        data = {"context_projection": {
            "omitted": True, "source_result_hash": digest,
            "tool_call_id": call_id, "reason": "consumed_tool_result_above_soft_target",
            "source_bytes": len(text.encode("utf-8")),
        }, "source": locators, "excerpt_prefix": "", "excerpt_suffix": ""}
        view = result.model_copy(update={"data": data, "content_blocks": [], "provider": None})
        # Include the complete ToolResult envelope in fitting, including CJK
        # byte costs, evidence references and JSON escaping.
        low, high = 0, len(text)
        while low < high:
            length = (low + high + 1) // 2
            head = (length + 1) // 2
            tail = length // 2
            data["excerpt_prefix"] = text[:head]
            data["excerpt_suffix"] = text[-tail:] if tail else ""
            if _tokens(view.model_dump(mode="json")) <= max_tokens:
                low = length
            else:
                high = length - 1
        data["excerpt_prefix"] = text[:(low + 1) // 2]
        data["excerpt_suffix"] = text[-(low // 2):] if low // 2 else ""
        encoded = view.model_dump(mode="json")
        if _tokens(encoded) >= _tokens(raw):
            continue
        projected[index] = {**message, "content": _json(encoded)}
        stats.append({
            "tool_call_id": call_id, "tool_name": name, "source_result_hash": digest,
            "original_tokens": _tokens(raw), "projected_tokens": _tokens(encoded),
            "budget_unmet": _tokens(encoded) > max_tokens,
            "reason": "consumed_tool_result_above_soft_target",
        })
    return projected, stats


@dataclass(frozen=True)
class BudgetedContext:
    context: ContextPackage
    count: RequestTokenCount
    web_projection: list[dict]
    tool_projection: list[dict]
    count_calls: int


async def fit_context_budget(
    package: ContextPackage, *, provider, provider_type: str,
    tools: list[dict], parameters: dict,
    invoke_summary=None, force_compaction: bool = False,
) -> BudgetedContext:
    """Project, clean consumed results, then trim atomic History groups.

    A hard overflow goes to bounded semantic compaction before history is lost.
    Cached summaries are projections over verified canonical source ranges.
    Current input, active state and unconsumed results remain protected.
"""

    raw = list(package.source_messages if package.source_messages is not None else package.messages)
    sources = request_manifest(raw, package.source_manifest or package.manifest)
    cached = apply_hard_compactions(replace(
        package, messages=tuple(raw), manifest=tuple(sources),
        source_messages=tuple(raw), source_manifest=tuple(sources),
    ))
    cache_applied = cached.messages != tuple(raw)
    web_stats, tool_stats = [], []
    messages = list(cached.messages)
    if package.hard_input_limit is not None:
        messages, web_stats = project_web_messages(
            messages, hard_input_limit=package.hard_input_limit,
            budget_ratio=package.web_result_budget_ratio,
            preserve_evidence=package.context_policy_key == "context.evidence",
        )

    count_calls = 0

    async def count(value):
        nonlocal count_calls
        count_calls += 1
        return await count_provider_request(
            provider, provider_type=provider_type, messages=value, tools=tools, parameters=parameters,
        )

    counted = await count(messages)
    stages = []
    if package.soft_input_target is not None and counted.tokens > package.soft_input_target:
        messages, tool_stats = project_consumed_tool_results(
            messages, recent_tool_rounds=package.recent_tool_rounds,
            max_tokens=package.consumed_tool_result_tokens,
        )
        if tool_stats:
            before = counted.tokens
            counted = await count(messages)
            stages.append({"action": "reduce_consumed_tool_results", "before_tokens": before,
                           "after_tokens": counted.tokens, "results": tool_stats})
    if force_compaction or (package.hard_input_limit is not None
                            and counted.tokens > package.hard_input_limit):
        compacted = await compact_context(
            replace(cached, messages=tuple(messages),
                    manifest=tuple(request_manifest(messages, cached.manifest))),
            provider=provider, provider_type=provider_type,
            tools=tools, parameters=parameters, invoke_summary=invoke_summary,
            force=force_compaction,
        )
        counted = await count(compacted.messages)
        return BudgetedContext(replace(
            compacted, estimated_tokens=counted.tokens, pre_call_tokens=counted.tokens,
            count_method=counted.method,
        ), counted, web_stats, [], count_calls)
    view = replace(cached, messages=tuple(messages), manifest=tuple(request_manifest(messages, cached.manifest)))
    assembler = ContextAssembler()
    # Canonical indexes deliberately remain unchanged by a summary projection.
    # Never use those indexes to trim the shorter projected message list.
    for target, floor, reason in (() if cache_applied else (
        (package.soft_input_target, package.recent_history_messages, "provider_count_above_soft_target"),
    )):
        removed = 0
        before = counted.tokens
        if target is not None and counted.tokens > target:
            candidates = [view]
            while True:
                candidate = candidates[-1]
                size = oldest_history_group_size(candidate)
                if not size or candidate.history_count - size < floor:
                    break
                candidates.append(assembler.drop_oldest_history(candidate, reason=reason))
            # Search complete history groups, not individual records. Native
            # counting may require network I/O: use O(log N) count requests,
            # then retain only a candidate whose actual count was checked.
            low, high = 1, len(candidates) - 1
            chosen = high
            counts = {0: counted}
            while low <= high:
                middle = (low + high) // 2
                counts[middle] = await count(candidates[middle].messages)
                if counts[middle].tokens <= target:
                    chosen, high = middle, middle - 1
                else:
                    low = middle + 1
            if chosen > 0:
                counted = counts.get(chosen) or await count(candidates[chosen].messages)
                candidate = candidates[chosen]
                removed = view.history_count - candidate.history_count
                start = view.history_start
                del raw[start:start + removed]
                del sources[start:start + removed]
                view = candidate
        if removed:
            stages.append({"action": "trim_history", "reason": reason, "removed_messages": removed,
                           "before_tokens": before, "after_tokens": counted.tokens})
    compaction = dict(view.compaction or {})
    if compaction or stages or counted.tokens > (package.soft_input_target or counted.tokens):
        compaction.update({
            "policy_key": package.context_policy_key,
            "stages": [*(compaction.get("stages") or []), *stages][-32:],
            "latest_pass_stages": stages,
            "soft_target_unmet": counted.tokens > (package.soft_input_target or counted.tokens),
        })
        compaction.pop("content_hash", None)
        compaction["content_hash"] = hashlib.sha256(_json(compaction).encode()).hexdigest()
    return BudgetedContext(
        replace(view, source_messages=tuple(raw), source_manifest=tuple(request_manifest(raw, sources)),
                estimated_tokens=counted.tokens, pre_call_tokens=counted.tokens,
                count_method=counted.method, compaction=compaction or None),
        counted, web_stats, tool_stats, count_calls,
    )
