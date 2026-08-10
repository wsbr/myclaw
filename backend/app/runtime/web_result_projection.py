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

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "full_result_hash": self.full_result_hash,
            "full_chars": self.full_chars,
            "full_tokens": self.full_tokens,
            "projected_chars": self.projected_chars,
            "projected_tokens": self.projected_tokens,
            "omitted": self.omitted,
            "consumed": self.consumed,
            "omission_reason": self.omission_reason,
        }


def _compact_provider(provider: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(provider, dict):
        return None
    return {
        key: provider[key]
        for key in ("name", "model", "search_strategy", "request_id")
        if provider.get(key) is not None
    }


def _fit_texts(
    values: Iterable[str], *, base: dict[str, Any], max_tokens: int,
) -> list[str]:
    selected: list[str] = []
    for value in values:
        text = str(value)
        if not text:
            continue
        candidate = [*selected, text]
        if _estimated_tokens({**base, "excerpts": candidate}) <= max_tokens:
            selected = candidate
            continue
        remaining = max_tokens * 2
        used = sum(len(item) for item in selected)
        if remaining - used > 128:
            selected.append(text[:remaining - used])
        break
    return selected


def project_web_result(
    tool_name: str, result: ToolResult, *, max_tokens: int,
    consumed: bool = False,
) -> ToolResult:
    """Return a ToolResult-shaped model view while leaving the input untouched."""

    if tool_name not in WEB_TOOL_NAMES or not result.success:
        return result
    data = dict(result.data or {})
    if tool_name == "web_search":
        projected_items = []
        for item in list(data.get("results") or []):
            if not isinstance(item, dict):
                continue
            snippet = str(item.get("snippet") or "")
            projected_items.append({
                "source_id": item.get("source_id"),
                "title": item.get("title") or item.get("url") or "",
                "url": item.get("url") or "",
                **({"published_at": item.get("published_at")} if item.get("published_at") else {}),
                "retrieved_at": data.get("retrieved_at") or "unknown",
                "excerpts": [snippet] if snippet else [],
                "omitted": True,
            })
        snippet_limit = 160 if consumed else 600
        for item in projected_items:
            item["excerpts"] = [
                str(value)[:snippet_limit] for value in item.get("excerpts") or []
            ]
        projected_data = {
            "results": projected_items,
            "count": len(projected_items),
            "omitted": True,
        }
        while projected_items and _estimated_tokens(projected_data) > max_tokens:
            projected_items.pop()
            projected_data["count"] = len(projected_items)
        def build_search_projection() -> ToolResult:
            citations = [{
                "type": "citation", "url": item["url"],
                "title": item.get("title") or item["url"],
                **({"text": item["excerpts"][0]} if item.get("excerpts") else {}),
            } for item in projected_items if item.get("url")]
            return result.model_copy(update={
                "data": projected_data,
                "content_blocks": citations,
                "evidence": [{
                    "kind": "search_result", "source_id": item.get("source_id"),
                    "url": item.get("url"), "rank": index + 1,
                } for index, item in enumerate(projected_items)],
                "provider": _compact_provider(result.provider),
            })

        projected = build_search_projection()
        while projected_items and _estimated_tokens(projected.model_dump(mode="json")) > max_tokens:
            projected_items.pop()
            projected_data["count"] = len(projected_items)
            projected = build_search_projection()
        return projected

    objective = str(data.get("objective") or "Extract the main factual content.")
    excerpts = [str(item) for item in list(data.get("excerpts") or []) if item]
    if not excerpts and data.get("text"):
        excerpts = select_relevant_excerpts(str(data["text"]), objective)
    if consumed:
        excerpts = excerpts[:1]
        excerpts = [item[:600] for item in excerpts]
    base = {
        "title": data.get("title"),
        "source_id": data.get("source_id") or "web-" + hashlib.sha256(
            str(data.get("final_url") or data.get("url") or "unknown").encode()
        ).hexdigest()[:20],
        "url": data.get("final_url") or data.get("url") or "",
        **({"published_at": data.get("published_at")} if data.get("published_at") else {}),
        "retrieved_at": data.get("retrieved_at") or "unknown",
        "omitted": True,
    }
    selected = _fit_texts(
        excerpts, base=base,
        max_tokens=max(1, max_tokens - (220 if consumed else 400)),
    )
    projected_data = {**base, "excerpts": selected}
    citation = {
        "type": "citation", "url": projected_data["url"],
        "title": projected_data.get("title") or projected_data["url"],
    }
    def build_fetch_projection() -> ToolResult:
        return result.model_copy(update={
            "data": projected_data,
            "content_blocks": [citation],
            "evidence": ([] if consumed else [{
                "kind": "web_fetch", "source_id": projected_data["source_id"],
                "url": projected_data["url"],
                "untrusted_external_content": True,
            }]),
            "provider": _compact_provider(result.provider),
        })

    projected = build_fetch_projection()
    while selected and _estimated_tokens(projected.model_dump(mode="json")) > max_tokens:
        selected.pop()
        projected = build_fetch_projection()
    return projected


def project_web_messages(
    messages: Iterable[dict[str, Any]], *, hard_input_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project every Web result under one shared, newest-first budget."""

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
        BALANCED_WEB_MAX_TOKENS,
        max(256, int(hard_input_limit * BALANCED_WEB_INPUT_FRACTION)),
    )
    allocations: dict[int, int] = {}
    remaining = total_budget
    prioritized = sorted(
        candidates, key=lambda item: (item[4], -item[0]),
    )
    floor = max(96, min(384, total_budget // max(1, len(prioritized))))
    for position, (index, _call_id, tool_name, _result, consumed) in enumerate(prioritized):
        cap = (
            BALANCED_SINGLE_FETCH_TOKENS
            if tool_name == "web_fetch" else BALANCED_SINGLE_SEARCH_TOKENS
        )
        if consumed:
            cap = min(cap, 512)
        reserve = floor * (len(prioritized) - position - 1)
        allocation = min(cap, max(floor, remaining - reserve))
        allocations[index] = allocation
        remaining = max(0, remaining - allocation)

    stats: list[dict[str, Any]] = []
    for index, call_id, tool_name, result, consumed in candidates:
        raw_content = str(source[index].get("content") or "")
        canonical_full = _json(result.model_dump(mode="json"))
        projected = project_web_result(
            tool_name, result, max_tokens=allocations[index], consumed=consumed,
        )
        semantically_omitted = (
            projected.model_dump(mode="json") != result.model_dump(mode="json")
        )
        projected_content = _json(projected.model_dump(mode="json"))
        source[index] = {
            **source[index],
            "content": projected_content,
        }
        stats.append(WebProjectionStat(
            tool_name=tool_name,
            tool_call_id=call_id,
            full_result_hash=hashlib.sha256(canonical_full.encode()).hexdigest(),
            full_chars=len(raw_content),
            full_tokens=_estimated_tokens(result.model_dump(mode="json")),
            projected_chars=len(projected_content),
            projected_tokens=_estimated_tokens(projected.model_dump(mode="json")),
            omitted=semantically_omitted,
            consumed=consumed,
            omission_reason=(
                "consumed_citation_digest" if consumed
                else "balanced_model_projection" if semantically_omitted
                else "none"
            ),
        ).as_dict())
    return source, stats
