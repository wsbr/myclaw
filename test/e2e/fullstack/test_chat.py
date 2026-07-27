"""Full-stack: Chat mode with real worker + trace validation."""

import pytest

from e2e.fullstack.conftest import requires_fullstack
from shared.fullstack_helpers import seed_ready_agent

pytestmark = [pytest.mark.fullstack, requires_fullstack]


@pytest.mark.asyncio
async def test_chat_stream_produces_response_and_trace(fs_client):
    agent = await seed_ready_agent(fs_client, modes=["chat"])
    session = await fs_client.create_session(agent["id"], mode="chat")

    response = await fs_client.chat(session["id"], "Hello, what can you do?")
    assert response
    assert "Mock assistant reply" in response

    trace = await fs_client.fetch_full_trace(session["id"])
    runners = trace["runners"]
    assert len(runners) >= 1

    runner = runners[0]
    assert runner["status"] == "completed"

    spans = trace["spans_by_runner"][runner["id"]]
    llm_spans = [s for s in spans if s["span_type"] == "llm_call"]
    assert len(llm_spans) >= 1
    assert llm_spans[0]["status"] == "completed"
