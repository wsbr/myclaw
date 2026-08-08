"""Phase-one Run budget profile invariants."""

import pytest

from app.services.run_reliability_service import (
    default_run_budget,
    normalize_run_budget,
)

pytestmark = pytest.mark.unit


def test_group_budget_has_bounded_peer_collaboration_headroom():
    group = default_run_budget("group")
    direct = default_run_budget("chat")

    assert group["max_tokens"] == 512_000
    assert group["max_tokens"] > direct["max_tokens"]
    assert group["max_agent_turns"] == 160
    assert group["max_provider_attempts"] == 240
    assert group["max_tool_invocations"] == 128
    assert group["max_active_seconds"] == 1_800


def test_existing_run_budget_snapshot_is_not_rewritten_by_new_default():
    historical = normalize_run_budget({"max_tokens": 400_000}, mode="group")
    invalid = normalize_run_budget({"max_tokens": 0}, mode="group")

    assert historical["max_tokens"] == 400_000
    assert invalid["max_tokens"] == 512_000
