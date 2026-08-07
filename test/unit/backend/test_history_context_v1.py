"""History projection, budget, and per-membership summary tests."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.agent_v1 import AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import OutboxCommand, PlatformAccount, UserIdentity, UserSession
from app.models.group_v1 import GroupMembership, GroupSession
from app.models.history_v1 import HistorySummary
from app.models.registry import PolicyDefinition, RegisteredModel
from app.models.runtime_v1 import DurableRunner, Message, Run, WorkItem
from app.services.history_service import select_history

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_group_history_is_membership_scoped_and_summary_is_namespaced():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, agent_1, agent_2 = uuid7(), uuid7(), uuid7()
    session_id, group_id, node_1, node_2 = uuid7(), uuid7(), uuid7(), uuid7()
    run_id, work_id, runner_id, model_id = uuid7(), uuid7(), uuid7(), uuid7()

    async with factory.begin() as db:
        db.add(UserIdentity(user_id="u1", status="active"))
        db.add(PlatformAccount(id=account_id, user_id="u1", display_name="U", status="active"))
        db.add(RegisteredModel(id=model_id, key="summary-model", provider="openai_compatible",
            model_name="summary-model", default_parameters={}, capabilities={}, enabled=True, config_version=1))
        db.add_all([
            AgentBlueprint(id=agent_1, owner_account_id=account_id, visibility="enterprise", status="published", next_version_no=1),
            AgentBlueprint(id=agent_2, owner_account_id=account_id, visibility="enterprise", status="published", next_version_no=1),
        ])
        db.add(UserSession(id=session_id, user_id="u1", entry_agent_id=agent_1, status="active", next_event_sequence=9))
        db.add(GroupSession(id=group_id, session_id=session_id, user_id="u1",
                            initiator_agent_id=agent_1, status="open", goal="g", bootstrap_context="b"))
        db.add_all([
            GroupMembership(id=node_1, group_session_id=group_id, agent_id=agent_1,
                            status="joined", invited_by_kind="user", invited_by_account_id=account_id,
                            reason="selected", authorization_source="user_selected", joined_at=utcnow()),
            GroupMembership(id=node_2, group_session_id=group_id, agent_id=agent_2,
                            status="joined", invited_by_kind="user", invited_by_account_id=account_id,
                            reason="selected", authorization_source="user_selected", joined_at=utcnow()),
        ])
        run = Run(id=run_id, session_id=session_id, user_id="u1", mode="group",
                  status="active")
        work = WorkItem(id=work_id, run_id=run_id, session_id=session_id, user_id="u1", mode="group",
                        owner_agent_id=agent_1, agent_version_id=uuid7(), group_run_id=uuid7(),
                        group_membership_id=node_1, goal="g", status="running", execution_epoch=1, attempt_no=1)
        runner = DurableRunner(id=runner_id, work_item_id=work_id, run_id=run_id, session_id=session_id,
                               agent_id=agent_1, agent_version_id=work.agent_version_id,
                               execution_epoch=1, status="running", principal_snapshot={"group_membership_id": node_1},
                               assembly_snapshot={}, prompt_manifest={},
                               input_snapshot={"group_session_id": group_id, "group_membership_id": node_1})
        db.add_all([run, work, runner])
        visible_user = Message(id=uuid7(), session_id=session_id, user_id="u1", scope_type="group",
            group_session_id=group_id, sender_type="user", sender_user_id="u1", recipient_type="agent",
            recipient_membership_id=node_1, role="user", content="to one", status="committed")
        own_result = Message(id=uuid7(), session_id=session_id, user_id="u1", scope_type="group",
            group_session_id=group_id, sender_type="agent", sender_agent_id=agent_1,
            recipient_type="session", role="assistant", content="from one", status="committed")
        hidden_user = Message(id=uuid7(), session_id=session_id, user_id="u1", scope_type="group",
            group_session_id=group_id, sender_type="user", sender_user_id="u1", recipient_type="agent",
            recipient_membership_id=node_2, role="user", content="secret to two", status="committed")
        hidden_result = Message(id=uuid7(), session_id=session_id, user_id="u1", scope_type="group",
            group_session_id=group_id, sender_type="agent", sender_agent_id=agent_2,
            recipient_type="session", role="assistant", content="secret from two", status="committed")
        direct_to_one = Message(id=uuid7(), session_id=session_id, user_id="u1", scope_type="group",
            group_session_id=group_id, sender_type="agent", sender_agent_id=agent_2,
            recipient_type="agent", recipient_membership_id=node_1,
            role="assistant", content="direct to one", status="committed")
        db.add_all([visible_user, own_result, hidden_user, hidden_result, direct_to_one])
        policy = PolicyDefinition(id=uuid7(), policy_key="history.summary_recent", policy_type="history",
            name="Summary", description="Summary", implementation_ref="x", implementation_version="1",
            default_parameters={}, parameter_version=3, enabled=True)
        db.add(policy)
        await db.flush()
        selection = await select_history(db, run=run, runner=runner, policy=policy,
            parameters={"token_budget": 100, "max_messages": 80, "summary_trigger_tokens": 1,
                        "summary_recent_token_budget": 3}, model_id=model_id)
        # Until the asynchronous summary is ready, summary_recent safely
        # degrades to history.recent within the same membership projection.
        assert selection.manifest["selected_message_ids"] == [
            visible_user.id, own_result.id, direct_to_one.id,
        ]
        assert hidden_user.id not in selection.manifest["selected_message_ids"]
        assert hidden_result.id not in selection.manifest["selected_message_ids"]

    async with factory() as db:
        summary = await db.scalar(select(HistorySummary))
        command = await db.scalar(select(OutboxCommand).where(OutboxCommand.action == "summarize_history"))
        assert summary is not None and command is not None
        assert summary.scope_type == "group_agent"
        assert summary.group_session_id == group_id
        assert summary.group_membership_id == node_1
        assert summary.through_message_id == own_result.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_delegated_agent_does_not_receive_ambient_chat_history():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as db:
        db.add(UserIdentity(user_id="u1", status="active"))
        db.add(UserSession(id="s1", user_id="u1", entry_agent_id="a1", status="active"))
        run = Run(id="r1", session_id="s1", user_id="u1", mode="chat",
                  status="active")
        work = WorkItem(id="w1", run_id="r1", session_id="s1", user_id="u1", mode="chat",
                        owner_agent_id="a2", agent_version_id="v2", goal="delegated", status="running",
                        execution_epoch=1, attempt_no=1)
        runner = DurableRunner(id="rr1", work_item_id="w1", run_id="r1", session_id="s1", agent_id="a2",
            agent_version_id="v2", execution_epoch=1, status="running", principal_snapshot={},
            assembly_snapshot={}, prompt_manifest={}, input_snapshot={"agent_invocation_id": "invoke-1"})
        db.add_all([run, work, runner, Message(id="m1", session_id="s1", user_id="u1", scope_type="chat",
            sender_type="user", sender_user_id="u1", recipient_type="session", role="user",
            content="ambient secret", status="committed")])
        await db.flush()
        selection = await select_history(db, run=run, runner=runner, policy=None, parameters={}, model_id="model")
        assert selection.messages == []
        assert selection.manifest["selected_message_ids"] == []
    await engine.dispose()
