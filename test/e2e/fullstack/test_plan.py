"""Full-stack: Plan mode with real Celery worker + trace validation."""

import pytest

from e2e.fullstack.conftest import requires_fullstack
from shared.fullstack_helpers import EXPECTED_PLAN_STEPS, seed_ready_agent
from shared.trace_validator import TraceValidator

pytestmark = [pytest.mark.fullstack, requires_fullstack]

GOAL = "Analyze Q3 sales performance and summarize key trends"


@pytest.mark.asyncio
async def test_plan_full_execution_with_real_worker(fs_client):
    """Plan generate → execute via real worker → validate Task + Trace."""
    agent = await seed_ready_agent(fs_client, modes=["chat", "plan"])

    session = await fs_client.create_session(agent["id"], mode="plan")
    plan = await fs_client.generate_plan(session["id"], GOAL)
    assert plan["status"] == "draft"
    assert len(plan["steps"]) == EXPECTED_PLAN_STEPS

    exec_result = await fs_client.execute_plan(session["id"], plan["plan_id"])
    task = await fs_client.wait_for_task(exec_result["task_id"], timeout=240.0)

    assert task["status"] == "completed", task.get("output_data")

    final_plan = await fs_client.get_plan(session["id"], plan["plan_id"])
    trace = await fs_client.fetch_full_trace(session["id"])

    TraceValidator().validate_plan_execution(
        task=task,
        plan=final_plan,
        runners=trace["runners"],
        spans_by_runner=trace["spans_by_runner"],
        expected_step_count=EXPECTED_PLAN_STEPS,
    ).assert_ok()
