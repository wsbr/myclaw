"""Phase-one Run budget profile invariants."""

import pytest

from app.services.run_reliability_service import (
    BudgetExceeded,
    RunBudgetState,
    assert_post_provider_budget,
    assert_turn_budget,
    default_run_budget,
    normalize_run_budget,
)

pytestmark = pytest.mark.unit


def test_group_budget_has_bounded_peer_collaboration_headroom():
    group = default_run_budget("group")
    direct = default_run_budget("chat")

    assert group["max_tokens"] == 768_000
    assert group["max_tokens"] > direct["max_tokens"]
    assert group["max_agent_turns"] == 160
    assert group["max_provider_attempts"] == 240
    assert group["max_tool_invocations"] == 128
    assert group["max_active_seconds"] == 1_800


def test_direct_budget_has_real_plan_flow_token_headroom():
    chat = default_run_budget("chat")
    plan = default_run_budget("plan")

    assert chat["max_tokens"] == 200_000
    assert plan["max_tokens"] == 200_000
    assert plan["max_tokens"] < default_run_budget("group")["max_tokens"]


def test_existing_run_budget_snapshot_is_not_rewritten_by_new_default():
    historical_400k = normalize_run_budget({"max_tokens": 400_000}, mode="group")
    historical_512k = normalize_run_budget({"max_tokens": 512_000}, mode="group")
    invalid = normalize_run_budget({"max_tokens": 0}, mode="group")

    assert historical_400k["max_tokens"] == 400_000
    assert historical_512k["max_tokens"] == 512_000
    assert invalid["max_tokens"] == 768_000


@pytest.mark.asyncio
async def test_token_overage_is_settled_then_rejected_before_the_next_provider_call(
    monkeypatch,
):
    limits = {
        "max_agent_turns": 64,
        "max_provider_attempts": 96,
        "max_tool_invocations": 64,
        "max_agent_invocations": 24,
        "max_transport_retries": 8,
        "max_tokens": 100,
        "max_active_seconds": 900,
        "no_progress_turns": 4,
    }

    def state(tokens: int) -> RunBudgetState:
        return RunBudgetState(
            limits=limits,
            usage={
                "agent_turns": 1,
                "provider_attempts": 1,
                "tool_invocations": 0,
                "agent_invocations": 0,
                "transport_retries": 0,
                "tokens": tokens,
                "active_seconds": 1,
            },
            remaining_transport_retries=8,
            warning_dimensions=(),
        )

    current = state(99)

    async def load_state(_db, _run_id):
        return current

    monkeypatch.setattr(
        "app.services.run_reliability_service.load_run_budget_state",
        load_state,
    )

    # One real token remains, so the next provider call gets its full request.
    assert await assert_turn_budget(None, "run-1") is current

    # The completed call may cross the cap without discarding its result.
    current = state(140)
    assert await assert_post_provider_budget(None, "run-1") is current

    # Its persisted actual usage fences only the following provider call.
    with pytest.raises(BudgetExceeded) as captured:
        await assert_turn_budget(None, "run-1")
    assert captured.value.dimension == "tokens"
    assert captured.value.used == 140
    assert captured.value.limit == 100
