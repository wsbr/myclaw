"""Required-action decisions preserve the frozen Runner execution identity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.required_actions import decide_tool_confirmation
from app.deps import CurrentAccount
from app.models.common import utcnow
from app.models.runtime_v1 import DurableRunner, ToolConfirmation, ToolInvocation
from app.schemas.runtime_v1 import ToolConfirmationDecisionRequest

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_tool_approval_resume_preserves_group_runner_snapshots(monkeypatch):
    import app.api.required_actions as module

    digest = "a" * 64
    confirmation = ToolConfirmation(
        id="confirmation", tool_invocation_id="invocation", user_id="user",
        tool_key="write_file", arguments_hash=digest, principal_hash="b" * 64,
        status="pending", policy_version=3, version=1, created_at=utcnow(),
    )
    invocation = ToolInvocation(
        id="invocation", tenant_id="test-tenant", account_id="account",
        runner_id="source-runner", work_item_id="work",
        run_id="run", session_id="session", platform_tool_id="tool",
        tool_key="write_file", execution_mode="inline", status="waiting_confirmation",
        arguments={"path": "audit.txt", "content": "evidence"},
        arguments_hash=digest, business_idempotency_key="call",
        principal_hash="b" * 64, attempt_count=0, version=1,
    )
    source = DurableRunner(
        id="source-runner", tenant_id="test-tenant",
        work_item_id="work", run_id="run", session_id="session",
        agent_id="group-agent", agent_version_id="group-version", execution_epoch=4,
        status="succeeded",
        principal_snapshot={
            "tenant_id": "test-tenant", "account_id": "account",
            "user_id": "user", "mode": "group", "current_agent_id": "group-agent",
            "group_membership_id": "group-node", "delegation_chain": ["entry-agent"],
        },
        assembly_snapshot={"agent_version_id": "group-version", "content_hash": "hash"},
        prompt_manifest={},
        input_snapshot={
            "group_session_id": "group", "group_run_id": "group-run",
            "group_membership_id": "group-node", "goal": "audit refunds",
        },
    )
    run = SimpleNamespace(
        id="run", tenant_id="test-tenant", account_id="account",
        session_id="session", user_id="user", entry_agent_id="entry-agent",
        mode="group", status="waiting_tool", wait_reason="waiting_tool", version=2,
    )
    work = SimpleNamespace(
        id="work", owner_agent_id="group-agent", agent_version_id="group-version",
        execution_epoch=4, attempt_no=4, status="waiting_user", version=4,
    )
    record = SimpleNamespace(id="idempotency", response_snapshot=None)
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, confirmation, invocation, run, work, None]),
        get=AsyncMock(side_effect=[confirmation, invocation, source]),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    persist = AsyncMock()
    events = SimpleNamespace(append=AsyncMock(side_effect=[
        SimpleNamespace(id="decision-event"), SimpleNamespace(id="queued-event"),
    ]))
    outbox = SimpleNamespace(enqueue=AsyncMock())

    monkeypatch.setattr(module, "_idempotency", AsyncMock(return_value=(record, False)))
    monkeypatch.setattr(module, "persist_replacement_runner", persist)
    monkeypatch.setattr(module, "ExecutionEventWriter", lambda _db: events)
    monkeypatch.setattr(module, "OutboxWriter", lambda _db: outbox)

    response = await decide_tool_confirmation(
        "confirmation", "approve",
        ToolConfirmationDecisionRequest(arguments_hash=digest, reason="approved"),
        if_match='W/"1"', idempotency_key="approval-1", db=db,
        current=CurrentAccount(
            account=SimpleNamespace(
                id="account", tenant_id="test-tenant", user_id="user",
            ),
            roles=frozenset({"user"}),
        ),
    )

    replacement = persist.await_args.args[2]
    assert response.status == "approved"
    assert replacement.agent_id == source.agent_id
    assert replacement.agent_version_id == source.agent_version_id
    assert replacement.principal_snapshot == source.principal_snapshot
    assert replacement.assembly_snapshot == source.assembly_snapshot
    assert replacement.prompt_manifest == source.prompt_manifest
    assert replacement.input_snapshot == {
        **source.input_snapshot,
        "resumed_from_confirmation_id": confirmation.id,
    }
    assert replacement.input_snapshot["group_membership_id"] == "group-node"
    assert replacement.principal_snapshot["delegation_chain"] == ["entry-agent"]
