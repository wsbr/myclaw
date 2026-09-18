"""Balanced web retrieval, clock, and model-context governance."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.tool_registry.base import ToolResult
from app.core.tool_registry.registry import BUILTIN_TOOLS
from app.runtime.action_dispatcher import ActionDispatcher
from app.runtime.protocol import ToolCallItem
from app.runtime.runtime_facts import runtime_clock_snapshot, runtime_clock_system_rules
from app.runtime.token_counting import resolve_model_input_limits
from app.runtime.web_result_projection import project_web_messages
from app.schemas.registry_v1 import ModelVersionCreate
from app.services.web_tool_service import (
    _looks_like_soft_404,
    _normalize_search_items,
    effective_search_query,
    select_relevant_excerpts,
)
from app.workers.runtime_worker import _web_retrieval_runtime_rules


pytestmark = pytest.mark.unit


def _success(data: dict, *, text_block: str | None = None) -> dict:
    result = ToolResult(
        status="succeeded", summary="ok", data=data,
        content_blocks=([{"type": "text", "text": text_block}] if text_block else []),
    )
    return result.model_dump(mode="json")


def test_runner_clock_is_beijing_authority_and_relative_query_uses_exact_date():
    clock = runtime_clock_snapshot(datetime(2026, 8, 10, 16, 30, tzinfo=timezone.utc))
    assert clock == {
        "source": "runner_execution_started_at",
        "timezone": "Asia/Shanghai",
        "local_date": "2026-08-11",
        "local_datetime": "2026-08-11T00:30:00+08:00",
        "utc_offset": "+08:00",
        "year": 2026,
        "fact_hash": clock["fact_hash"],
    }
    assert "2026-08-11" in runtime_clock_system_rules(clock)
    effective, domains = effective_search_query(
        "今天的模型新闻", freshness="day",
        domains=["Example.com", "docs.example.com"], runtime_clock=clock,
    )
    assert "2026-08-11 through 2026-08-11" in effective
    assert "site:example.com OR site:docs.example.com" in effective
    assert domains == ["example.com", "docs.example.com"]
    week, _ = effective_search_query(
        "最近一周的模型新闻", freshness="week", runtime_clock=clock,
    )
    assert "2026-08-05 through 2026-08-11" in week
    assert len(clock["fact_hash"]) == 64

    timeless, _ = effective_search_query("Python descriptor protocol")
    assert timeless == "Python descriptor protocol"


def test_runtime_prompt_treats_run_ceiling_as_recovery_reserve():
    rules = _web_retrieval_runtime_rules({
        "web_search": {"total": 2, "succeeded": 2},
        "web_fetch": {"total": 1, "succeeded": 1},
    })
    assert "two web_search" in rules
    assert "hard Run ceilings" in rules
    assert '"succeeded": 2' in rules


def test_runner_clock_is_stable_but_cross_midnight_replacement_gets_new_snapshot():
    started = datetime(2026, 8, 10, 15, 59, tzinfo=timezone.utc)
    first = runtime_clock_snapshot(started)
    transparent_retry = runtime_clock_snapshot(started)
    replacement = runtime_clock_snapshot(
        datetime(2026, 8, 10, 16, 1, tzinfo=timezone.utc),
    )

    assert transparent_retry == first
    assert first["local_date"] == "2026-08-10"
    assert replacement["local_date"] == "2026-08-11"
    assert replacement["fact_hash"] != first["fact_hash"]


def test_qwen_thinking_context_uses_provider_input_ceiling_before_context_minus_output():
    limits = resolve_model_input_limits({
        "context_window": 1_000_000,
        "max_input_tokens": 983_616,
        "max_output_tokens": 65_536,
    }, {"max_tokens": 4_096})
    assert limits.hard_input_limit == 983_616
    assert limits.soft_input_target == int(983_616 * 0.8)


@pytest.mark.parametrize("capabilities", [
    {"context_window": 0},
    {"context_window": 100, "max_input_tokens": 101},
    {"context_window": 100, "max_output_tokens": 101},
    {"context_window": True},
])
def test_model_registry_rejects_incoherent_context_capabilities(capabilities):
    with pytest.raises(ValidationError):
        ModelVersionCreate(
            provider="openai_compatible", model_name="qwen3.5-plus",
            endpoint="https://example.com/v1", api_key="write-only",
            capabilities=capabilities,
        )


@pytest.mark.parametrize("default_parameters", [
    {"max_tokens": 0},
    {"max_tokens": True},
    {"max_tokens": 65_537},
])
def test_model_registry_rejects_invalid_default_output(default_parameters):
    with pytest.raises(ValidationError):
        ModelVersionCreate(
            provider="openai_compatible", model_name="qwen3.5-plus",
            endpoint="https://example.com/v1", api_key="write-only",
            default_parameters=default_parameters,
            capabilities={
                "context_window": 1_000_000,
                "max_input_tokens": 983_616,
                "max_output_tokens": 65_536,
            },
        )


def test_search_schema_and_fetch_schema_keep_context_knobs_out_of_model_control():
    search = BUILTIN_TOOLS["web_search"].input_schema
    fetch = BUILTIN_TOOLS["web_fetch"].input_schema
    assert set(search["properties"]) == {"query", "freshness", "domains"}
    assert search["properties"]["query"]["maxLength"] == 512
    assert set(fetch["properties"]) == {"url", "objective"}
    assert fetch["required"] == ["url", "objective"]


def test_search_results_are_canonical_deduplicated_and_source_diverse():
    items = _normalize_search_items([
        {"title": "a1", "url": "https://a.example/x?utm_source=test", "snippet": "1"},
        {"title": "duplicate", "url": "https://a.example/x", "snippet": "same"},
        {"title": "a2", "url": "https://a.example/y", "snippet": "2"},
        {"title": "a3", "url": "https://a.example/z", "snippet": "3"},
        {"title": "b1", "url": "https://b.test/one", "snippet": "4"},
        {"title": "b2", "url": "https://b.test/two", "snippet": "5"},
    ], limit=4)
    assert [item["title"] for item in items] == ["a1", "a2", "b1", "b2"]
    assert all(item["source_id"].startswith("web-") for item in items)


def test_objective_excerpts_support_chinese_and_soft_404_needs_joint_signals():
    text = "导航\n公司季度收入为 120 亿元，同比增长 15%。\n其他不相关内容。" * 20
    excerpts = select_relevant_excerpts(text, "季度收入和同比增长", max_chars=1_000)
    assert excerpts and "120 亿元" in excerpts[0]
    assert _looks_like_soft_404(
        final_url="https://example.com/404.html",
        title="Page not found", text="Return home",
    ) is True
    assert _looks_like_soft_404(
        final_url="https://example.com/reports/404-trends",
        title="Analysis of HTTP status codes", text="A valid research article",
    ) is False


def test_full_web_results_are_projected_under_shared_budget_without_losing_pairing():
    huge_search = _success({
        "query": "current", "original_query": "current",
        "effective_query": "current\n2026-08-10", "freshness": None,
        "domains": [], "retrieved_at": "2026-08-10T00:00:00+00:00",
        "count": 8, "results": [{
            "source_id": f"search-{index}", "title": f"Result {index}",
            "url": f"https://source{index}.example/item", "site_name": "example",
            "snippet": "search body " * 1_000,
        } for index in range(8)],
    })
    fetch_text = "irrelevant " * 30_000 + "OBJECTIVE-NEEDLE verified fact"
    huge_fetch = _success({
        "url": "https://evidence.example/report",
        "final_url": "https://evidence.example/report", "title": "Report",
        "mime_type": "text/html", "source_id": "fetch-source",
        "checksum": "a" * 64, "truncated": False,
        "text_length": len(fetch_text), "text": fetch_text,
        "retrieved_at": "2026-08-10T00:00:00+00:00",
        "objective": "OBJECTIVE-NEEDLE verified fact",
        "excerpts": ["OBJECTIVE-NEEDLE verified fact" + " evidence" * 2_000],
    }, text_block=fetch_text)
    messages = [{
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "search-call", "type": "tool_use", "name": "web_search", "input": {},
        }],
    }, {
        "role": "tool", "tool_call_id": "search-call", "tool_use_id": "search-call",
        "content": json.dumps(huge_search),
    }, {
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "fetch-call", "type": "tool_use", "name": "web_fetch", "input": {},
        }],
    }, {
        "role": "tool", "tool_call_id": "fetch-call", "tool_use_id": "fetch-call",
        "content": json.dumps(huge_fetch),
    }]

    projected, stats = project_web_messages(messages, hard_input_limit=30_000)
    assert [item["tool_call_id"] for item in stats] == ["search-call", "fetch-call"]
    assert stats[0]["consumed"] is True
    assert stats[1]["consumed"] is False
    assert all(item["omitted"] for item in stats)
    assert all(item["full_tokens"] >= item["projected_tokens"] for item in stats)
    assert stats[0]["omission_reason"] == "consumed_citation_digest"
    assert sum(item["projected_tokens"] for item in stats) <= 6_200
    fetch_result = json.loads(projected[-1]["content"])
    assert "text" not in fetch_result["data"]
    assert set(fetch_result["data"]) == {
        "source_id", "title", "url", "retrieved_at", "excerpts", "omitted", "checksum",
    }
    assert "OBJECTIVE-NEEDLE" in json.dumps(fetch_result, ensure_ascii=False)
    assert projected[-1]["tool_call_id"] == "fetch-call"


def test_failed_web_result_can_be_rehydrated_and_projected():
    failed = ToolResult(
        status="failed",
        summary="Fetch failed",
        error="Tool execution failed: ConnectError",
        error_code="tool_execution_unhandled",
        error_category="provider",
    )
    messages = [{
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "fetch-failed", "type": "tool_use", "name": "web_fetch",
            "input": {"url": "https://example.com", "objective": "verify"},
        }],
    }, {
        "role": "tool", "tool_use_id": "fetch-failed",
        "tool_call_id": "fetch-failed",
        "content": json.dumps(failed.model_dump(mode="json")),
    }]

    projected, stats = project_web_messages(messages, hard_input_limit=100_000)

    restored = ToolResult.model_validate(json.loads(projected[1]["content"]))
    assert restored.error is not None
    assert restored.error.code == "tool_execution_unhandled"
    assert len(stats) == 1


def test_four_parallel_full_fetches_share_budget_and_do_not_overflow_old_context():
    full_text = "long source evidence " * 20_000
    messages = [{
        "role": "assistant", "content": "", "tool_calls": [{
            "id": f"fetch-{index}", "type": "tool_use", "name": "web_fetch",
            "input": {"url": f"https://source{index}.example/report", "objective": "evidence"},
        } for index in range(4)],
    }]
    for index in range(4):
        result = _success({
            "final_url": f"https://source{index}.example/report", "title": "Report",
            "mime_type": "text/html", "source_id": f"source-{index}",
            "checksum": str(index) * 64, "truncated": True,
            "text_length": len(full_text), "text": full_text,
            "retrieved_at": "2026-08-10T00:00:00+00:00",
            "objective": "evidence", "excerpts": [full_text],
        }, text_block=full_text)
        messages.append({
            "role": "tool", "tool_use_id": f"fetch-{index}",
            "tool_call_id": f"fetch-{index}", "content": json.dumps(result),
        })

    projected, stats = project_web_messages(messages, hard_input_limit=28_672)

    assert len(stats) == 4
    assert sum(item["projected_tokens"] for item in stats) <= int(28_672 * 0.2)
    assert all("text" not in json.loads(item["content"])["data"] for item in projected[1:])
    assert json.loads(projected[-1]["content"])["data"]["excerpts"]

    concise, concise_stats = project_web_messages(
        messages, hard_input_limit=28_672, budget_ratio=0.10,
    )
    evidence, evidence_stats = project_web_messages(
        messages, hard_input_limit=28_672, budget_ratio=0.35,
    )
    assert sum(item["projected_tokens"] for item in concise_stats) < sum(
        item["projected_tokens"] for item in evidence_stats
    )
    assert [item["tool_call_id"] for item in concise[1:]] == [
        item["tool_call_id"] for item in evidence[1:]
    ] == [f"fetch-{index}" for index in range(4)]
    assert all(json.loads(item["content"])["data"]["text"] == full_text for item in messages[1:])


@pytest.mark.asyncio
async def test_balanced_harness_rejects_excess_batch_without_admission(monkeypatch):
    class Database:
        async def scalar(self, _statement):
            return SimpleNamespace()

        async def scalars(self, _statement):
            return []

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.runtime.action_dispatcher._record_authorization_rejection", no_record,
    )
    dispatcher = ActionDispatcher([])
    calls = [
        ToolCallItem(
            call_id=f"call-{index}", name="web_search",
            arguments={"query": "x"}, index=index,
        )
        for index in range(3)
    ]
    admitted, rejected = await dispatcher._apply_web_retrieval_limits(
        calls, db=Database(),
        runtime_context=SimpleNamespace(session_id="session"),
        run_id="run", source_turn_id="turn",
    )
    assert [item.call_id for item in admitted] == ["call-0", "call-1"]
    assert rejected["call-2"].result.error.code == "web_retrieval_limit_exceeded"


@pytest.mark.asyncio
async def test_run_recovery_reserve_requires_a_prior_typed_failure(monkeypatch):
    class Database:
        async def scalar(self, _statement):
            return SimpleNamespace()

        async def scalars(self, _statement):
            return [
                SimpleNamespace(
                    tool_key="web_search", source_turn_id=f"prior-{index}",
                    provider_tool_call_id=f"prior-call-{index}", status="succeeded",
                )
                for index in range(2)
            ]

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.runtime.action_dispatcher._record_authorization_rejection", no_record,
    )
    dispatcher = ActionDispatcher([])
    call = ToolCallItem(
        call_id="third-search", name="web_search",
        arguments={"query": "another query"}, index=0,
    )

    admitted, rejected = await dispatcher._apply_web_retrieval_limits(
        [call], db=Database(), runtime_context=SimpleNamespace(session_id="session"),
        run_id="run", source_turn_id="new-turn",
    )

    assert admitted == []
    assert rejected["third-search"].result.error.details == {
        "dimension": "run_recovery_reserve", "limit": 2, "used": 2,
    }

    class FailedDatabase(Database):
        async def scalars(self, _statement):
            rows = await super().scalars(_statement)
            rows[-1].status = "failed"
            return rows

    admitted, rejected = await dispatcher._apply_web_retrieval_limits(
        [call], db=FailedDatabase(),
        runtime_context=SimpleNamespace(session_id="session"),
        run_id="run", source_turn_id="new-turn",
    )
    assert admitted == [call]
    assert rejected == {}
