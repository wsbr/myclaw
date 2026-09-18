"""Web evidence stays traceable when shared projection budgets are tight."""

from copy import deepcopy
import json

import pytest

from app.core.tool_registry.base import ToolResult
from app.runtime.web_result_projection import project_web_messages, project_web_result


pytestmark = pytest.mark.unit


def _fetch(index=0, *, excerpts=None, url=None):
    return ToolResult(success=True, summary="Fetched reference", data={
        "source_id": f"source-{index}",
        "url": url or f"https://source{index}.example/report",
        "title": f"Reference {index}",
        "checksum": str(index % 10) * 64,
        "normalized_text_checksum": "f" * 64,
        "retrieved_at": "2026-09-17T10:00:00+00:00",
        "excerpts": excerpts or ["Evidence about the requested fact. " * 1000],
    })


def _messages(results, *, name="web_fetch"):
    messages = []
    for index, result in enumerate(results):
        messages.extend([{
            "role": "assistant", "content": "", "tool_calls": [{
                "id": f"call-{index}", "name": name, "input": {},
            }],
        }, {
            "role": "tool", "tool_call_id": f"call-{index}",
            "content": result.model_dump_json(),
        }])
    return messages


def _data(messages):
    return [json.loads(item["content"])["data"] for item in messages if item["role"] == "tool"]


def test_shared_budget_reserves_all_source_references_before_excerpt_allocation():
    messages = _messages([_fetch(index) for index in range(4)])
    before = deepcopy(messages)
    projected, stats = project_web_messages(
        messages, hard_input_limit=20_000, budget_ratio=0.1,
        preserve_evidence=True,
    )
    assert sum(item["projected_tokens"] for item in stats) <= 2000
    assert sum(item["allocated_tokens"] for item in stats) <= 2000
    assert all(not item["shared_budget_exceeded"] for item in stats)
    assert [item["source_id"] for item in _data(projected)] == [f"source-{i}" for i in range(4)]
    assert all(item["checksum"] == str(i) * 64 for i, item in enumerate(_data(projected)))
    assert all(item["retrieved_at"] == "2026-09-17T10:00:00+00:00" for item in _data(projected))
    assert [item["tool_call_id"] for item in projected if item["role"] == "tool"] == [f"call-{i}" for i in range(4)]
    assert messages == before


def test_many_sources_report_required_metadata_overage_instead_of_claiming_budget_fit():
    messages = _messages([_fetch(index) for index in range(40)])
    projected, stats = project_web_messages(messages, hard_input_limit=10_000, budget_ratio=0.1)
    total = sum(item["projected_tokens"] for item in stats)
    assert total > 1000
    assert all(item["shared_budget_tokens"] == 1000 for item in stats)
    assert all(item["shared_projected_tokens"] == total for item in stats)
    assert all(item["shared_budget_exceeded"] for item in stats)
    assert all(item["unavoidable_metadata_tokens"] == total - 1000 for item in stats)
    assert all(not item["excerpts"] for item in _data(projected))
    assert len({item["source_id"] for item in _data(projected)}) == 40


def test_extreme_reference_url_is_not_truncated_to_hide_budget_overage():
    url = "https://source.example/" + "long-path" * 1000
    result = _fetch(url=url)
    projected, stats = project_web_messages(_messages([result]), hard_input_limit=2000, budget_ratio=0.1)
    assert _data(projected)[0]["url"] == url
    assert _data(projected)[0]["checksum"] == result.data["checksum"]
    assert stats[0]["shared_budget_exceeded"] is True
    assert stats[0]["unavoidable_metadata_tokens"] > 0
    assert _data(projected)[0]["excerpts"] == []


def test_short_result_is_not_expanded_or_marked_as_omitted():
    result = _fetch(excerpts=["The value is 42."])
    original = result.model_dump(mode="json")
    assert project_web_result("web_fetch", result, max_tokens=1000).model_dump(mode="json") == original
    projected, stats = project_web_messages(_messages([result]), hard_input_limit=10_000)
    assert json.loads(projected[1]["content"]) == original
    assert stats[0]["projected_tokens"] == stats[0]["full_tokens"]
    assert stats[0]["omitted"] is False


def test_repeated_search_projection_keeps_existing_excerpts_and_source_checksums():
    result = ToolResult(success=True, summary="Search completed", data={
        "retrieved_at": "2026-09-17", "results": [{
            "source_id": "search-1", "url": "https://search.example/report",
            "title": "Report", "checksum": "a" * 64,
            "snippet": "Important search evidence. " * 1000,
        }],
    })
    first, _ = project_web_messages(_messages([result], name="web_search"), hard_input_limit=5000, budget_ratio=.1)
    second, _ = project_web_messages(first, hard_input_limit=5000, budget_ratio=.1)
    assert second == first
    third, _ = project_web_messages(second, hard_input_limit=4000, budget_ratio=.1)
    item = _data(third)[0]["results"][0]
    assert item["excerpts"] and "Important search" in item["excerpts"][0]
    assert item["checksum"] == "a" * 64
    assert item["retrieved_at"] == "2026-09-17"


def test_chinese_excerpt_fits_serialized_budget_without_corrupting_reference_identity():
    result = _fetch(excerpts=["季度收入为120亿元，同比增长15%。" * 1000])
    projected, stats = project_web_messages(_messages([result]), hard_input_limit=10_000, budget_ratio=.1)
    assert stats[0]["projected_tokens"] <= 1000
    assert stats[0]["shared_budget_exceeded"] is False
    assert _data(projected)[0]["excerpts"][0].startswith("季度收入为120亿元")
    assert _data(projected)[0]["normalized_text_checksum"] == "f" * 64
    assert result.data["excerpts"][0].endswith("同比增长15%。")


def test_evidence_mode_keeps_consumed_conflicting_passages_when_budget_allows():
    passages = ["Source A: the claim is supported. " * 35, "Source B: the claim is contradicted. " * 35]
    result = _fetch(excerpts=passages)
    messages = _messages([result, _fetch(1, excerpts=["Follow-up source."])])
    balanced, balanced_stats = project_web_messages(messages, hard_input_limit=50_000)
    evidence, evidence_stats = project_web_messages(messages, hard_input_limit=50_000, preserve_evidence=True)
    assert balanced_stats[0]["consumed"] is evidence_stats[0]["consumed"] is True
    assert balanced_stats[0]["projected_tokens"] <= 512
    assert "contradicted" not in str(_data(balanced)[0])
    assert _data(evidence)[0]["excerpts"] == passages
    assert evidence_stats[0]["projected_tokens"] > 512
    assert sum(item["projected_tokens"] for item in evidence_stats) <= 10_000


def test_search_projection_preserves_all_source_references_under_tight_budget():
    result = ToolResult(success=True, summary="Search completed", data={
        "retrieved_at": "2026-09-17", "results": [{
            "source_id": f"search-{index}", "url": f"https://source{index}.example/",
            "snippet": ("supports" if index == 0 else "contradicts") * 1000,
        } for index in range(6)],
    })
    projected, stats = project_web_messages(_messages([result], name="web_search"), hard_input_limit=15_000, budget_ratio=.1, preserve_evidence=True)
    items = _data(projected)[0]["results"]
    assert {item["source_id"] for item in items} == {f"search-{i}" for i in range(6)}
    assert stats[0]["projected_tokens"] <= 1500
    assert stats[0]["shared_budget_exceeded"] is False
