"""ORM contract: no deprecated phase-one orchestration projection is active."""

import pytest

from app.database import Base


pytestmark = pytest.mark.unit


def test_agent_native_schema_has_one_run_lifecycle_and_no_v1_graph_tables():
    legacy_tables = {
        "plan_execution", "plan_work_item_node", "plan_work_item_dependency",
        "plan_declared_agent", "group_formation_proposal",
        "group_formation_revision", "group_formation_participant",
        "group_formation_work_item", "group_formation_suggested_edge",
        "group_agent_node", "group_initial_suggested_edge",
        "group_run_agent_version", "group_collaboration_edge",
    }
    assert legacy_tables.isdisjoint(Base.metadata.tables)

    run_columns = set(Base.metadata.tables["run"].columns.keys())
    assert {"status", "wait_reason", "terminal_reason", "completed_at"} <= run_columns
    assert {
        "lifecycle", "outcome", "execution_kind", "entry_agent_id",
        "source_run_id", "result_summary", "error_code", "started_at",
        "finished_at",
    }.isdisjoint(run_columns)
    assert "first_payload_at" in Base.metadata.tables["provider_attempt"].columns
    assert "schema_repair_count" in Base.metadata.tables["agent_turn"].columns

    group_columns = set(Base.metadata.tables["group_session"].columns.keys())
    assert {"session_id", "initiator_agent_id", "membership_policy"} <= group_columns
    assert {
        "parent_session_id", "frozen_formation_revision_id", "entry_agent_id",
        "confirmed_at",
    }.isdisjoint(group_columns)

    assert "group_membership_id" in Base.metadata.tables["work_item"].columns
    assert "recipient_membership_id" in Base.metadata.tables["message"].columns
    assert "group_membership_id" in Base.metadata.tables["clarification"].columns
    assert "published_by_membership_id" in Base.metadata.tables["group_artifact"].columns
