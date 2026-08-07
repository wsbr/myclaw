"""Agent Network -> direct Group -> peer collaboration vertical contract."""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.groups import create_group, get_group_interaction_graph
from app.core.tool_registry.base import RuntimeContext
from app.core.tool_registry.builtin.search_group_history import SearchGroupHistoryTool
from app.database import Base
from app.deps import SessionActor
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint, AgentCard
from app.models.common import utcnow, uuid7
from app.models.foundation import ExecutionEvent, PlatformAccount, UserIdentity, UserSession
from app.models.group_v1 import (
    GroupMembership,
    GroupRun,
    GroupRunMember,
    GroupSummaryRequest,
)
from app.models.runtime_v1 import AgentInvocation, DurableRunner, Message, Run, WorkItem
from app.schemas.group_v1 import GroupCreateRequest
from app.services.agent_invocation_service import dispatch_agent_invocation
from app.services.group_collaboration_service import (
    reject_fixed_run_invitation,
    send_agent_group_message,
)
from app.workers.runtime_worker import RuntimeWorker


pytestmark = pytest.mark.unit


def _published_agent(account_id: str, name: str, capability: str):
    agent_id, version_id = uuid7(), uuid7()
    agent = AgentBlueprint(
        id=agent_id,
        owner_account_id=account_id,
        visibility="enterprise",
        status="published",
        current_published_version_id=version_id,
        next_version_no=2,
    )
    version = AgentAssemblyVersion(
        id=version_id,
        agent_id=agent_id,
        version_no=1,
        publication_status="published",
        name=name,
        description=f"{name} UAT professional",
        instructions=f"Work as the {name} peer.",
        group_enabled=True,
        discovery_config={
            "domain_tags": [name.casefold()],
            "supported_outcomes": ["capacity-risk-calculator"],
        },
        collaboration_config={
            "discoverable": True,
            "callable": True,
            "governance": {"group_allowed": True, "labels": ["uat"]},
        },
        created_by_account_id=account_id,
        published_at=utcnow(),
        immutable_at=utcnow(),
        content_hash=capability[0] * 64,
    )
    card = AgentCard(
        id=uuid7(),
        agent_id=agent_id,
        agent_version_id=version_id,
        visibility="enterprise",
        name=name,
        description=version.description,
        identity_kind="agent",
        domain_tags=[name.casefold()],
        supported_outcomes=["capacity-risk-calculator"],
        accepted_request_summary=f"Own {name} work",
        capabilities_summary={"tool_keys": [capability], "capabilities": [capability]},
        collaboration_summary=version.collaboration_config,
        declaration={"role": name},
        attestation={"assembly_content_hash": version.content_hash},
        availability="available",
        group_enabled=True,
        content_hash=capability[-1] * 64,
        published_at=version.published_at,
    )
    return agent, version, card


@pytest.mark.asyncio
async def test_direct_group_creation_is_10_of_10_from_network_members():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, session_id = uuid7(), uuid7()
    agents = [
        _published_agent(account_id, "Product", "requirements"),
        _published_agent(account_id, "Development", "workspace_write"),
        _published_agent(account_id, "Test", "test_execute"),
    ]
    async with factory.begin() as db:
        account = PlatformAccount(
            id=account_id, user_id="uat-user", display_name="UAT", status="active",
        )
        db.add_all([
            UserIdentity(user_id="uat-user", status="active"),
            account,
            *[row for bundle in agents for row in bundle],
            UserSession(
                id=session_id, user_id="uat-user", account_id=account_id,
                entry_agent_id=agents[0][0].id, status="active", origin_type="web",
                next_event_sequence=1,
            ),
        ])
    actor = SessionActor(account=account, roles=frozenset({"user"}))
    member_ids = [bundle[0].id for bundle in agents]

    created = []
    for attempt in range(10):
        async with factory() as db:
            result = await create_group(
                session_id,
                GroupCreateRequest(
                    member_agent_ids=member_ids,
                    reporter_agent_id=member_ids[0],
                    goal=f"Capacity risk calculator acceptance attempt {attempt + 1}",
                ),
                idempotency_key=f"group-uat-{attempt + 1}",
                db=db,
                actor=actor,
            )
            created.append(result)

    assert len({item["id"] for item in created}) == 10
    assert all(item["membership_policy"] == "fixed_for_run" for item in created)
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(GroupMembership)) == 30
        assert await db.scalar(select(func.count()).select_from(GroupRun)) == 10
        assert await db.scalar(select(func.count()).select_from(Run)) == 10
        assert await db.scalar(select(func.count()).select_from(GroupRunMember)) == 30
        assert await db.scalar(select(func.count()).select_from(DurableRunner)) == 10
        creation_events = list(await db.scalars(select(ExecutionEvent).where(
            ExecutionEvent.event_type == "group.created",
        )))
        assert len(creation_events) == 10
    await engine.dispose()


@pytest.mark.asyncio
async def test_group_peers_message_invoke_audit_and_repeat_history():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id, session_id = uuid7(), uuid7()
    agents = [
        _published_agent(account_id, "Product", "requirements"),
        _published_agent(account_id, "Development", "workspace_write"),
        _published_agent(account_id, "Test", "test_execute"),
    ]
    async with factory.begin() as db:
        account = PlatformAccount(
            id=account_id, user_id="peer-user", display_name="Peer UAT", status="active",
        )
        db.add_all([
            UserIdentity(user_id="peer-user", status="active"), account,
            *[row for bundle in agents for row in bundle],
            UserSession(
                id=session_id, user_id="peer-user", account_id=account_id,
                entry_agent_id=agents[0][0].id, status="active", origin_type="web",
                next_event_sequence=1,
            ),
        ])
    actor = SessionActor(account=account, roles=frozenset({"user"}))
    product_id, development_id, test_id = [bundle[0].id for bundle in agents]
    async with factory() as db:
        group_view = await create_group(
            session_id,
            GroupCreateRequest(
                member_agent_ids=[product_id, development_id, test_id],
                reporter_agent_id=product_id,
                goal="Build and verify the capacity risk calculator",
            ),
            idempotency_key="peer-group",
            db=db,
            actor=actor,
        )

    async with factory.begin() as db:
        group_run = await db.scalar(select(GroupRun).where(
            GroupRun.group_session_id == group_view["id"],
        ))
        run = await db.get(Run, group_run.run_id)
        root_work = await db.get(WorkItem, run.root_work_item_id)
        product_runner = await db.get(DurableRunner, root_work.active_runner_id)
        memberships = {
            item.agent_id: item for item in await db.scalars(select(GroupMembership).where(
                GroupMembership.group_session_id == group_view["id"],
            ))
        }
        product_context = RuntimeContext(
            session_id=session_id, runner_id=product_runner.id, user_id="peer-user",
            account_id=account_id, agent_id=product_id,
            group_session_id=group_view["id"], group_run_id=group_run.id,
            group_membership_id=memberships[product_id].id,
        )
        _message, development_runner = await send_agent_group_message(
            db, context=product_context, run_id=run.id, work_item_id=root_work.id,
            content="Implement calculator and return code for review",
            recipient_agent_id=development_id, visibility="direct",
            operation_key="product-to-development",
        )
        development_work = await db.get(WorkItem, development_runner.work_item_id)
        development_context = RuntimeContext(
            session_id=session_id, runner_id=development_runner.id, user_id="peer-user",
            account_id=account_id, agent_id=development_id,
            group_session_id=group_view["id"], group_run_id=group_run.id,
            group_membership_id=memberships[development_id].id,
        )
        _reverse_message, reverse_product_runner = await send_agent_group_message(
            db, context=development_context, run_id=run.id,
            work_item_id=development_work.id,
            content="Implementation ready; requirements review requested",
            recipient_agent_id=product_id, visibility="direct",
            operation_key="development-to-product",
        )
        await send_agent_group_message(
            db, context=development_context, run_id=run.id,
            work_item_id=development_work.id,
            content="Calculator implementation ready for integration test",
            recipient_agent_id=None, visibility="group",
            operation_key="development-shared",
        )
        invocation = await dispatch_agent_invocation(
            db, operation_key="development-to-test-invocation",
            caller_runner=development_runner, caller_work=development_work,
            run=run,
            arguments={
                "agent_id": test_id,
                "task": "Execute boundary tests and reverse-review the calculator",
                "input": {"acceptance": "capacity and utilization risk"},
            },
        )
        rejected = await reject_fixed_run_invitation(
            db, context=development_context, run_id=run.id,
            work_item_id=development_work.id, target_agent_id=uuid7(),
            reason="Need a fourth specialist", operation_key="external-invite",
        )
        assert invocation.target_agent_id == test_id
        assert rejected["status"] == "rejected"
        assert rejected["reason_code"] == "membership_policy_fixed_for_run"

        # Settle actual peer work rather than fabricating graph edges. The
        # fourth Turn is the natural reverse message back to Product.
        worker = RuntimeWorker(worker_id="group-unit")
        context = SimpleNamespace(manifest=(), compaction=None)
        for runner, work, content in (
            (product_runner, root_work, "Product acceptance and risk formula ready"),
            (development_runner, development_work, "Calculator program implemented"),
            (
                reverse_product_runner,
                await db.get(WorkItem, reverse_product_runner.work_item_id),
                "Product reverse review accepted the implementation",
            ),
        ):
            await worker._settle_group_completed(
                db, runner=runner, work=work, run=run,
                content=content, context=context,
            )
        test_work = await db.get(WorkItem, invocation.target_work_item_id)
        test_runner = await db.get(DurableRunner, test_work.active_runner_id)
        await worker._settle_agent_target(
            db, runner=test_runner, work=test_work, run=run,
            invocation=invocation,
            content="Boundary tests passed; high-utilization risk is correctly flagged",
        )

        # Quiescence is idempotent per summary epoch and closes through the
        # selected Reporter, never through a permanent Supervisor.
        await worker._evaluate_group_quiescence(
            db, group_run_id=group_run.id, trigger_event_id=None,
        )
        await worker._evaluate_group_quiescence(
            db, group_run_id=group_run.id, trigger_event_id=None,
        )
        summary = await db.scalar(select(GroupSummaryRequest).where(
            GroupSummaryRequest.group_run_id == group_run.id,
        ))
        assert summary.summary_epoch == 1
        summary_work = await db.get(WorkItem, summary.reporter_work_item_id)
        summary_runner = await db.get(DurableRunner, summary_work.active_runner_id)
        await worker._settle_group_completed(
            db, runner=summary_runner, work=summary_work, run=run,
            content=(
                "Capacity risk calculator delivered with Product acceptance, "
                "Development implementation, and Test boundary verification."
            ),
            context=context,
        )
        assert run.status == "completed"
        assert run.status == "completed" and run.terminal_reason == "completed"
        assert group_run.delivered_summary_epoch == 1

    async with factory() as db:
        history_context = RuntimeContext(
            session_id=session_id, runner_id=uuid7(), user_id="peer-user",
            account_id=account_id, agent_id=product_id,
            group_session_id=group_view["id"], group_run_id=group_run.id,
            group_membership_id=memberships[product_id].id,
        )
        first = await SearchGroupHistoryTool().execute(
            {"query": "Calculator implementation", "limit": 20},
            history_context, db=db,
        )
        second = await SearchGroupHistoryTool().execute(
            {"query": "Calculator implementation", "limit": 20},
            history_context, db=db,
        )
        assert first.success and second.success
        assert first.data["hits"] == second.data["hits"]
        assert first.data["hits"]
        graph = await get_group_interaction_graph(group_run.id, db=db, actor=actor)
        facts = {
            (edge["source_agent_id"], edge["target_agent_id"], edge["cause_type"])
            for edge in graph["edges"]
        }
        assert (product_id, development_id, "message") in facts
        assert (development_id, product_id, "message") in facts
        assert (development_id, test_id, "agent_invocation") in facts
        assert await db.scalar(select(func.count()).select_from(Message).where(
            Message.group_run_id == group_run.id,
            Message.sender_agent_id.is_not(None),
        )) >= 7
        assert await db.scalar(select(func.count()).select_from(AgentInvocation).where(
            AgentInvocation.group_run_id == group_run.id,
        )) == 1
        invite_event = await db.scalar(select(ExecutionEvent).where(
            ExecutionEvent.group_run_id == group_run.id,
            ExecutionEvent.event_type == "group.membership_invitation_rejected",
        ))
        assert invite_event.payload["result"] == "rejected"
    await engine.dispose()
