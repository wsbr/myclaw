"""Bounded model projections for durable Web Tool evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from app.core.tool_registry.base import ToolResult
from app.services.web_tool_service import select_relevant_excerpts


WEB_TOOL_NAMES = {"web_search", "web_fetch"}
BALANCED_WEB_MAX_TOKENS = 12_000
BALANCED_WEB_INPUT_FRACTION = 0.20
BALANCED_SINGLE_FETCH_TOKENS = 4_000
BALANCED_SINGLE_SEARCH_TOKENS = 2_000


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )


def _estimated_tokens(value: Any) -> int:
    encoded = _json(value)
    return max(1, len(encoded) // 2, len(encoded.encode("utf-8")) // 4)


def _result_tokens(result: ToolResult) -> int:
    return _estimated_tokens(result.model_dump(mode="json"))


@dataclass(frozen=True)
class WebProjectionStat:
    tool_name: str
    tool_call_id: str
    full_result_hash: str
    full_chars: int
    full_tokens: int
    projected_chars: int
    projected_tokens: int
    omitted: bool
    consumed: bool
    omission_reason: str
    allocated_tokens: int
    minimum_reference_tokens: int
    budget_exceeded: bool
    shared_budget_tokens: int
    shared_projected_tokens: int
    shared_budget_exceeded: bool
    unavoidable_metadata_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def _compact_provider(provider: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(provider, dict):
        return None
    return {
        key: provider[key]
        for key in ("name", "model", "search_strategy", "request_id")
        if provider.get(key) is not None
    }


def _reference(data: dict[str, Any], *, retrieved_at: Any = None) -> dict[str, Any]:
    """Keep source identifiers verbatim; a digest is never a substitute for a URL."""

    url = data.get("final_url") or data.get("url") or ""
    reference = {
        "source_id": data.get("source_id") or "web-" + hashlib.sha256(
            str(url or "unknown").encode()
        ).hexdigest()[:20],
        "url": url,
        "retrieved_at": data.get("retrieved_at") or retrieved_at or "unknown",
        "omitted": True,
    }
    for key in ("checksum", "normalized_text_checksum", "published_at"):
        if data.get(key) is not None:
            reference[key] = data[key]
    return reference


def project_web_result(
    tool_name: str, result: ToolResult, *, max_tokens: int,
    consumed: bool = False, preserve_evidence: bool = False,
) -> ToolResult:
    """Return a bounded view without changing durable evidence.

    The minimum view retains all source references. If those references cannot
    fit, return them intact; callers report the overage and enforce the complete
    request's hard limit. A projection never grows an already shorter result.
    """

    if tool_name not in WEB_TOOL_NAMES or not result.success:
        return result
    limit = max(0, int(max_tokens))
    digest_consumed = consumed and not preserve_evidence
    if digest_consumed:
        limit = min(limit, 512)
    original_tokens = _result_tokens(result)
    if original_tokens <= limit:
        return result
    if not isinstance(result.data, dict):
        return result
    data = dict(result.data)
    references: list[dict[str, Any]] = []
    titles: list[Any] = []
    excerpts: list[list[str]] = []
    if tool_name == "web_search":
        for item in list(data.get("results") or []):
            if not isinstance(item, dict):
                continue
            references.append(_reference(item, retrieved_at=data.get("retrieved_at")))
            titles.append(item.get("title") or item.get("url") or "")
            # Already-projected results carry excerpts instead of snippets.
            existing = item.get("excerpts")
            values = (
                [str(value) for value in existing if value]
                if isinstance(existing, list)
                else [str(item["snippet"])] if item.get("snippet") else []
            )
            if not preserve_evidence:
                values = [value[:160 if digest_consumed else 600] for value in values]
            excerpts.append(values)
    else:
        references.append(_reference(data))
        titles.append(data.get("title"))
        values = [str(item) for item in list(data.get("excerpts") or []) if item]
        if not values and data.get("text"):
            values = select_relevant_excerpts(
                str(data["text"]),
                str(data.get("objective") or "Extract the main factual content."),
            )
        if digest_consumed:
            values = [value[:600] for value in values[:1]]
        excerpts.append(values)

    def build(selected: list[list[str]], *, rich: bool) -> ToolResult:
        items = [{
            **reference,
            **({"title": titles[index]} if rich else {}),
            "excerpts": list(selected[index]),
        } for index, reference in enumerate(references)]
        projected_data = (
            {"results": items, "count": len(items), "omitted": True}
            if tool_name == "web_search" else items[0]
        )
        citations = [{
            "type": "citation", "url": item["url"],
            "title": item.get("title") or item["url"],
        } for item in items if item.get("url")] if rich else []
        evidence = [{
            "kind": "search_result" if tool_name == "web_search" else "web_fetch",
            "source_id": item["source_id"], "url": item["url"],
            "untrusted_external_content": True,
        } for item in items] if rich else []
        return result.model_copy(update={
            "summary": result.summary[:240] if rich else "",
            "data": projected_data, "content_blocks": citations,
            "evidence": evidence, "timing": {},
            "provider": _compact_provider(result.provider) if rich else None,
        })

    selected: list[list[str]] = [[] for _ in references]
    minimum = build(selected, rich=False)
    if _result_tokens(minimum) >= limit:
        return minimum if _result_tokens(minimum) < original_tokens else result
    rich = _result_tokens(build(selected, rich=True)) <= limit
    projected = build(selected, rich=rich)
    # Fit the serialized envelope, including UTF-8 and citation overhead. Binary
    # search only slices excerpt text, never a URL, checksum, or source identity.
    for index, values in enumerate(excerpts):
        for value in values:
            selected[index].append(value)
            candidate = build(selected, rich=rich)
            if _result_tokens(candidate) <= limit:
                projected = candidate
                continue
            selected[index].pop()
            low, high = 0, len(value)
            while low < high:
                middle = (low + high + 1) // 2
                selected[index].append(value[:middle])
                fits = _result_tokens(build(selected, rich=rich)) <= limit
                selected[index].pop()
                if fits:
                    low = middle
                else:
                    high = middle - 1
            if low:
                selected[index].append(value[:low])
            projected = build(selected, rich=rich)
            break
    return projected if _result_tokens(projected) < original_tokens else result


def project_web_messages(
    messages: Iterable[dict[str, Any]], *, hard_input_limit: int,
    budget_ratio: float = BALANCED_WEB_INPUT_FRACTION,
    preserve_evidence: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Share one budget; report when required source references alone exceed it."""

    source = [dict(item) for item in messages]
    names: dict[str, str] = {}
    assistant_positions: list[int] = []
    for index, message in enumerate(source):
        calls = message.get("tool_calls")
        if message.get("role") == "assistant" and isinstance(calls, list):
            assistant_positions.append(index)
            for call in calls:
                if not isinstance(call, dict):
                    continue
                call_id = call.get("id")
                name = call.get("name") or (call.get("function") or {}).get("name")
                if call_id and name:
                    names[str(call_id)] = str(name)
    candidates = []
    for index, message in enumerate(source):
        if message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or message.get("tool_use_id") or "")
        tool_name = names.get(call_id)
        if tool_name not in WEB_TOOL_NAMES:
            continue
        try:
            parsed = json.loads(str(message.get("content") or "{}"))
            result = ToolResult.model_validate(parsed)
        except (ValueError, TypeError):
            continue
        consumed = any(position > index for position in assistant_positions)
        candidates.append((index, call_id, tool_name, result, consumed))

    total_budget = min(
        BALANCED_WEB_MAX_TOKENS, max(1, int(hard_input_limit * budget_ratio)),
    )
    # Reserve real minimum-envelope costs instead of an arbitrary per-item floor
    # that can spend more than the shared budget before excerpts are allocated.
    minimums = {
        index: _result_tokens(project_web_result(
            tool_name, result, max_tokens=0, consumed=consumed,
            preserve_evidence=preserve_evidence,
        ))
        for index, _call_id, tool_name, result, consumed in candidates
    }
    minimum_total = sum(minimums.values())
    remaining = max(0, total_budget - minimum_total)
    allocations: dict[int, int] = {}
    prioritized = sorted(candidates, key=lambda item: (item[4], -item[0]))
    for index, _call_id, tool_name, result, consumed in prioritized:
        cap = (
            BALANCED_SINGLE_FETCH_TOKENS
            if tool_name == "web_fetch" else BALANCED_SINGLE_SEARCH_TOKENS
        )
        if consumed and not preserve_evidence:
            cap = min(cap, 512)
        extra = min(remaining, max(0, min(cap, _result_tokens(result)) - minimums[index]))
        allocations[index] = minimums[index] + extra
        remaining -= extra

    projected_results = {
        index: project_web_result(
            tool_name, result, max_tokens=allocations[index], consumed=consumed,
            preserve_evidence=preserve_evidence,
        )
        for index, _call_id, tool_name, result, consumed in candidates
    }
    shared_tokens = sum(_result_tokens(result) for result in projected_results.values())
    stats: list[dict[str, Any]] = []
    for index, call_id, tool_name, result, consumed in candidates:
        raw_content = str(source[index].get("content") or "")
        canonical_full = _json(result.model_dump(mode="json"))
        projected = projected_results[index]
        semantically_omitted = projected.model_dump(mode="json") != result.model_dump(mode="json")
        projected_content = _json(projected.model_dump(mode="json"))
        source[index] = {**source[index], "content": projected_content}
        projected_tokens = _result_tokens(projected)
        stats.append(WebProjectionStat(
            tool_name=tool_name, tool_call_id=call_id,
            full_result_hash=hashlib.sha256(canonical_full.encode()).hexdigest(),
            full_chars=len(raw_content), full_tokens=_result_tokens(result),
            projected_chars=len(projected_content), projected_tokens=projected_tokens,
            omitted=semantically_omitted, consumed=consumed,
            omission_reason=(
                "consumed_citation_digest"
                if consumed and not preserve_evidence and semantically_omitted
                else "balanced_model_projection" if semantically_omitted else "none"
            ),
            allocated_tokens=allocations[index],
            minimum_reference_tokens=minimums[index],
            budget_exceeded=projected_tokens > allocations[index],
            shared_budget_tokens=total_budget, shared_projected_tokens=shared_tokens,
            shared_budget_exceeded=shared_tokens > total_budget,
            unavoidable_metadata_tokens=max(0, minimum_total - total_budget),
        ).as_dict())
    return source, stats
