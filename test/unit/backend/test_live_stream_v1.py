"""Ephemeral Session streaming remains ordered and never controls Run success."""

import pytest

from app.core.tool_registry.base import RuntimeContext, ToolResult
from app.runtime.action_dispatcher import ActionDispatcher
from app.runtime.live_stream import LiveStreamPublisher
from app.runtime.protocol import ToolCallItem


pytestmark = pytest.mark.unit


def _identity() -> dict[str, str]:
    return {
        "session_id": "session-1", "run_id": "run-1",
        "runner_id": "runner-1", "agent_turn_id": "turn-1",
    }


@pytest.mark.asyncio
async def test_first_delta_is_immediate_and_later_deltas_are_coalesced(monkeypatch):
    publisher = LiveStreamPublisher(flush_seconds=60, flush_chars=128)
    emitted = []

    async def capture(_session_id, payload):
        emitted.append(payload)

    monkeypatch.setattr(publisher, "_publish", capture)
    await publisher.publish_delta(**_identity(), delta="first")
    await publisher.publish_delta(**_identity(), delta=" second")
    await publisher.publish_delta(**_identity(), delta=" third")

    assert [(item["sequence"], item["delta"]) for item in emitted] == [
        (1, "first"),
    ]

    await publisher.flush(**_identity())
    assert [(item["sequence"], item["delta"]) for item in emitted] == [
        (1, "first"), (2, " second third"),
    ]


@pytest.mark.asyncio
async def test_reset_discards_pending_text_but_commit_flushes_it(monkeypatch):
    publisher = LiveStreamPublisher(flush_seconds=60, flush_chars=128)
    emitted = []

    async def capture(_session_id, payload):
        emitted.append(payload)

    monkeypatch.setattr(publisher, "_publish", capture)
    await publisher.publish_delta(**_identity(), delta="visible")
    await publisher.publish_delta(**_identity(), delta=" must-not-flash")
    await publisher.finish(
        **_identity(), event_type="model.stream_reset", reason_code="failed",
    )

    assert [item["event_type"] for item in emitted] == [
        "model.delta", "model.stream_reset",
    ]
    assert all(item.get("delta") != " must-not-flash" for item in emitted)

    emitted.clear()
    await publisher.publish_delta(**_identity(), delta="final")
    await publisher.publish_delta(**_identity(), delta=" tail")
    await publisher.finish(
        **_identity(), event_type="model.stream_committed", message_id="message-1",
    )

    assert [item["event_type"] for item in emitted] == [
        "model.delta", "model.delta", "model.stream_committed",
    ]
    assert emitted[-1]["message_id"] == "message-1"
    assert emitted[-1]["provisional"] is False


@pytest.mark.asyncio
async def test_inline_tool_progress_projects_running_and_terminal_status():
    progress = []

    async def observe(item):
        progress.append(item)

    async def execute(_name, _arguments, _context, _db):
        return ToolResult(status="succeeded", summary="safe summary")

    dispatcher = ActionDispatcher([{
        "name": "lookup", "input_schema": {"type": "object"},
        "output_schema": {}, "side_effect": "none",
        "confirmation_policy": "never", "execution_mode": "inline",
    }], executor=execute, progress_observer=observe)
    results = await dispatcher.dispatch_batch(
        [ToolCallItem(call_id="call-1", name="lookup", arguments={}, index=0)],
        capability_snapshot={"effective_tool_keys": ["lookup"]},
        runtime_context=RuntimeContext(
            session_id="session-1", runner_id="runner-1",
            user_id="user-1", agent_id="agent-1",
        ),
    )

    assert results[0].result.success
    assert [(item["tool_key"], item["status"]) for item in progress] == [
        ("lookup", "running"), ("lookup", "succeeded"),
    ]
