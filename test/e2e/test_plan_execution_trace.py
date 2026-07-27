"""
End-to-end test: Plan mode full execution chain.

Validates:
  Task status & output_data
  ExecutionPlan status & step outputs
  Runner hierarchy (plan_generate → plan parent → child runners)
  Span tree (plan_step spans + llm_call spans per step)
"""

import pytest

from integration.conftest import requires_services
from shared.trace_validator import TraceValidator

pytestmark = [pytest.mark.e2e, requires_services]

GOAL = "Analyze Q3 sales performance and summarize key trends"
EXPECTED_STEPS = 2


@pytest.mark.asyncio
async def test_plan_mode_full_execution_trace(e2e_client, plan_agent):
    """
    E2E: generate plan → execute → poll task → validate trace chain.

    Execution chain:
      POST plan/generate  → ExecutionPlan (draft) + plan_generate Runner + llm_call Span
      POST plan/execute   → Task → Celery → Parent Runner + plan_step Spans + Child Runners
    """
    client = e2e_client

    session = await client.create_session(plan_agent["id"], mode="plan")
    session_id = session["id"]

    plan = await client.generate_plan(session_id, GOAL)
    plan_id = plan["plan_id"]

    assert plan["status"] == "draft"
    assert len(plan["steps"]) == EXPECTED_STEPS
    assert plan["steps"][0]["id"] == "s1"
    assert plan["steps"][1]["depends_on"] == ["s1"]

    exec_result = await client.execute_plan(session_id, plan_id)
    task_id = exec_result["task_id"]

    task = await client.wait_for_task(task_id, timeout=90.0)
    assert task["status"] == "completed", f"Task failed: {task.get('output_data')}"

    final_plan = await client.get_plan(session_id, plan_id)
    trace = await client.fetch_full_trace(session_id)

    validator = TraceValidator()
    result = validator.validate_plan_execution(
        task=task,
        plan=final_plan,
        runners=trace["runners"],
        spans_by_runner=trace["spans_by_runner"],
        expected_step_count=EXPECTED_STEPS,
    )
    result.assert_ok()

    # Explicit assertions on execution results
    output = task.get("output_data") or {}
    assert "summary" in output
    assert all("Completed output for" in (s.get("output") or "") for s in output["steps"])

    # Trace structure spot-checks
    runners = trace["runners"]
    parent = next(r for r in runners if (r.get("input_data") or r.get("input") or {}).get("type") == "plan")
    children = [r for r in runners if r.get("parent_runner_id") == parent["id"]]
    assert len(children) == EXPECTED_STEPS

    parent_spans = trace["spans_by_runner"][parent["id"]]
    plan_step_types = [s for s in parent_spans if s["span_type"] == "plan_step"]
    assert len(plan_step_types) == EXPECTED_STEPS


@pytest.mark.asyncio
async def test_plan_generate_creates_trace_spans(e2e_client, plan_agent):
    """Verify plan generation alone produces runner + llm_call span in trace."""
    client = e2e_client

    session = await client.create_session(plan_agent["id"], mode="plan")
    await client.generate_plan(session["id"], GOAL)

    trace = await client.fetch_full_trace(session["id"])
    generate_runners = [
        r for r in trace["runners"]
        if (r.get("input_data") or r.get("input") or {}).get("mode") == "plan_generate"
    ]
    assert len(generate_runners) == 1

    spans = trace["spans_by_runner"][generate_runners[0]["id"]]
    llm_spans = [s for s in spans if s["span_type"] == "llm_call"]
    assert len(llm_spans) >= 1
    assert llm_spans[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_plan_step_outputs_propagate_to_task(e2e_client, plan_agent):
    """Verify each step's LLM output is captured in task output_data.steps[].output."""
    client = e2e_client

    session = await client.create_session(plan_agent["id"], mode="plan")
    plan = await client.generate_plan(session["id"], GOAL)
    exec_result = await client.execute_plan(session["id"], plan["plan_id"])

    task = await client.wait_for_task(exec_result["task_id"])
    steps = (task.get("output_data") or {}).get("steps", [])

    assert len(steps) == EXPECTED_STEPS
    for step in steps:
        assert step["status"] == "completed"
        assert step["output"]
        assert step["id"] in ("s1", "s2")
