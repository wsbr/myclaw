"""Full-stack: Trace API after plan execution."""

import pytest

from e2e.fullstack.conftest import requires_fullstack
from shared.fullstack_helpers import EXPECTED_PLAN_STEPS, seed_ready_agent

pytestmark = [pytest.mark.fullstack, requires_fullstack]


@pytest.mark.asyncio
async def test_trace_api_returns_decoded_spans(fs_client):
    agent = await seed_ready_agent(fs_client, modes=["plan"])
    session = await fs_client.create_session(agent["id"], mode="plan")
    plan = await fs_client.generate_plan(session["id"], "Test trace decoding")
    exec_result = await fs_client.execute_plan(session["id"], plan["plan_id"])
    await fs_client.wait_for_task(exec_result["task_id"], timeout=240.0)

    trace = await fs_client.fetch_full_trace(session["id"])
    assert len(trace["runners"]) >= 1 + EXPECTED_PLAN_STEPS  # generate + parent + children

    for runner in trace["runners"]:
        spans = trace["spans_by_runner"].get(runner["id"], [])
        for span in spans:
            # API should return decoded JSON, not raw bytes
            if span.get("snapshot") is not None:
                assert isinstance(span["snapshot"], dict)
            assert span["span_type"] in (
                "llm_call", "tool_call", "agent_call", "memory", "event", "plan_step"
            )
