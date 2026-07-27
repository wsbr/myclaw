"""Unit tests for plan executor pure functions."""

import pytest

from app.runtime.plan_executor import (
    parse_llm_json,
    normalize_steps,
    sort_plan_steps,
    build_step_prompt,
)

pytestmark = pytest.mark.unit


class TestParseLlmJson:
    def test_plain_json(self):
        result = parse_llm_json('{"goal": "test", "steps": []}')
        assert result["goal"] == "test"

    def test_json_code_block(self):
        raw = '```json\n{"goal": "x", "steps": [{"id": "s1"}]}\n```'
        result = parse_llm_json(raw)
        assert result["goal"] == "x"

    def test_generic_code_block(self):
        raw = '```\n{"goal": "y", "steps": []}\n```'
        result = parse_llm_json(raw)
        assert result["goal"] == "y"


class TestNormalizeSteps:
    def test_assigns_ids_and_defaults(self):
        steps = normalize_steps([{"description": "Do something"}])
        assert steps[0]["id"] == "s1"
        assert steps[0]["status"] == "pending"
        assert steps[0]["depends_on"] == []

    def test_preserves_existing_fields(self):
        steps = normalize_steps([{
            "id": "custom",
            "description": "Step A",
            "depends_on": ["s0"],
            "status": "completed",
            "output": "done",
        }])
        assert steps[0]["id"] == "custom"
        assert steps[0]["output"] == "done"


class TestSortPlanSteps:
    def test_topological_order(self):
        steps = [
            {"id": "s2", "description": "B", "depends_on": ["s1"]},
            {"id": "s1", "description": "A", "depends_on": []},
            {"id": "s3", "description": "C", "depends_on": ["s2"]},
        ]
        ordered = sort_plan_steps(steps)
        assert [s["id"] for s in ordered] == ["s1", "s2", "s3"]

    def test_cycle_falls_back_to_original(self):
        steps = [
            {"id": "s1", "description": "A", "depends_on": ["s2"]},
            {"id": "s2", "description": "B", "depends_on": ["s1"]},
        ]
        ordered = sort_plan_steps(steps)
        assert [s["id"] for s in ordered] == ["s1", "s2"]

    def test_ignores_unknown_dependencies(self):
        steps = [
            {"id": "s1", "description": "A", "depends_on": ["missing"]},
            {"id": "s2", "description": "B", "depends_on": []},
        ]
        ordered = sort_plan_steps(steps)
        assert len(ordered) == 2


class TestBuildStepPrompt:
    def test_includes_goal_and_step(self):
        prompt = build_step_prompt(
            {"id": "s1", "description": "Collect data", "depends_on": []},
            {},
            "Analyze sales",
        )
        assert "Analyze sales" in prompt
        assert "s1" in prompt
        assert "Collect data" in prompt

    def test_includes_dependency_outputs(self):
        prompt = build_step_prompt(
            {"id": "s2", "description": "Summarize", "depends_on": ["s1"]},
            {"s1": "Raw data result"},
            "Analyze sales",
        )
        assert "Output from step s1" in prompt
        assert "Raw data result" in prompt
