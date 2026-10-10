"""Real MySQL: sealed collaboration edges, runtime tools and Group emergence.

Requires an explicitly selected migrated disposable database. No provider is
called, and foreign keys and immutable-version triggers remain enabled.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.tool_registry.base import RuntimeContext
from app.models.agent_v1 import AgentAssemblyVersion, AgentVersionBinding, AgentVersionTool
from app.models.common import utcnow, uuid7
from app.models.foundation import OutboxCommand, PlatformAccount, UserIdentity, UserSession
from app.models.group_v1 import GroupMembership, GroupRunMember
from app.models.registry import (
    ConformanceRun, ModelVersion, PolicyDefinition, RegisteredModel, SandboxProfile, ToolGovernance,
)
from app.models.runtime_v1 import (
    AgentInvocation, AgentTurn, DurableRunner, Message, ProviderAttempt, Run,
    RunnerToolSnapshot, ToolInvocation, WorkItem,
)
from app.models.tenant_v1 import Tenant, TenantMembership
from app.persistence.reliable_writes import ExecutionEventWriter
from app.persistence.runtime_graph import persist_new_work_runner
from app.runtime.action_dispatcher import ActionDispatcher
from app.runtime.protocol import ToolCallItem
from app.runtime.snapshot_resolver import SnapshotResolver
from app.services.agent_assembly_service import (
    AssemblySelection, create_agent, publish_agent, update_agent_draft,
)
from app.services.group_collaboration_service import (
    build_group_emergence_candidate_snapshot, emerge_group_after_chat,
)
from app.services.registry_reconcile import builtin_tool_specs, reconcile_platform_tools, seed_core_registries
from app.services.run_followup_service import enqueue_run_terminal_followups
from app.services.run_reliability_service import default_run_budget

pytestmark = pytest.mark.integration


@pytest.fixture
async def mysql_collaboration():
    url = os.getenv("MYCLAW_V3_ACCEPTANCE_DATABASE_URL")
    if not url:
        pytest.skip("Select an isolated migrated MySQL with MYCLAW_V3_ACCEPTANCE_DATABASE_URL")
    if not (make_url(url).database or "").endswith("_test"):
        raise ValueError("collaboration integration requires a disposable *_test database")
    engine = create_async_engine(url, isolation_level="READ COMMITTED")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid7().replace("-", "")
    try:
        async with factory() as db:
            assert await db.scalar(text("SELECT @@foreign_key_checks")) == 1
            assert await db.scalar(text(
                "SELECT COUNT(*) FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE='agent_version_binding'"
            )) > 0
            tenant = Tenant(id=uuid7(), slug=f"collaboration-{suffix}", name="Collaboration integration")
            users = [UserIdentity(user_id=f"collaboration-{suffix}-{index}") for index in range(2)]
            db.add_all([tenant, *users])
            await db.flush()
            accounts = [PlatformAccount(
                id=uuid7(), tenant_id=tenant.id, user_id=user.user_id, display_name=f"Test user {index}",
            ) for index, user in enumerate(users)]
            db.add_all(accounts)
            await db.flush()
            db.add_all([TenantMembership(
                id=uuid7(), tenant_id=tenant.id, account_id=account.id, role_key="member", status="active",
            ) for account in accounts])
            model = RegisteredModel(
                id=uuid7(), key=f"collaboration-{suffix}", provider="openai", model_name="no-provider-fixture",
                enabled=True, status="active",
            )
            db.add(model)
            await db.flush()
            model_version = ModelVersion(
                id=uuid7(), model_id=model.id, version_no=1, provider="openai",
                model_name=model.model_name, content_hash="a" * 64, capabilities={"tool_calling": True},
            )
            db.add(model_version)
            await db.flush()
            model.current_version_id = model_version.id
            # Synthetic conformance is scoped to this fixture model only. Agent
            # publication and its exact frozen model/tool/policy bindings are real.
            db.add(ConformanceRun(
                id=uuid7(), subject_type="model_version", subject_id=model_version.id,
                suite_version=settings.provider_conformance_suite_version, idempotency_key=uuid7(),
                request_hash="a" * 64, status="passed", started_at=utcnow(), ended_at=utcnow(), result_summary={},
            ))
            await seed_core_registries(db)
            catalog = {row.tool_key: row for row in await reconcile_platform_tools(
                db, builtin_tool_specs(), job_run_id=f"collaboration:{suffix}",
            )}
            tool_ids = tuple(catalog[name].id for name in ("invoke_agent", "discover_agents", "select_group_members"))
            for tool_id in tool_ids:
                governance = await db.get(ToolGovernance, tool_id)
                governance.enabled = True
                governance.confirmation_policy = "never"
                governance.risk_rules = []
            policies = {row.policy_key: row for row in await db.scalars(select(PolicyDefinition))}
            sandbox = await db.scalar(select(SandboxProfile).where(SandboxProfile.profile_key == "local"))
            await db.commit()
            fixture = SimpleNamespace(
                factory=factory, tenant_id=tenant.id, account_id=accounts[0].id,
                other_account_id=accounts[1].id, user_id=users[0].user_id,
                model_id=model.id, tool_ids=tool_ids, sandbox_id=sandbox.id,
                policies={key: value.id for key, value in policies.items()},
            )
        yield fixture
    finally:
        await engine.dispose()


async def publish(db, fixture, name, *, network=False, bound=(), owner=None):
    owner = owner or fixture.account_id
    agent, draft = await create_agent(
        db, tenant_id=fixture.tenant_id, owner_account_id=owner, created_by_account_id=owner,
        name=name, description=f"{name} collaboration integration", visibility="private",
        initial_values={
            "instructions": "Use only authorized collaboration edges.", "model_id": fixture.model_id,
            "sandbox_profile_id": fixture.sandbox_id, "group_enabled": network,
            "context_policy_id": fixture.policies["context.standard"],
            "history_policy_id": fixture.policies["history.recent"],
            "memory_write_policy_id": fixture.policies["memory_write.none"],
            "memory_recall_policy_id": fixture.policies["memory_recall.none"],
            "collaboration_config": {"discoverable": True, "callable": True, "governance": {"group_allowed": True}},
        },
        selection=AssemblySelection(tool_ids=fixture.tool_ids, bound_agent_ids=tuple(bound)),
    )
    version = await publish_agent(
        db, agent_id=agent.id, expected_draft_revision=draft.draft_revision,
        actor_account_id=owner, idempotency_record_id=uuid7(),
    )
    return SimpleNamespace(agent=agent, version=version, draft=draft)


async def runtime_probe(db, *, runner):
    run = await db.get(Run, runner.run_id)
    work = await db.get(WorkItem, runner.work_item_id)
    resolved = await SnapshotResolver().resolve(
        db, runner=runner, execution_principal=runner.principal_snapshot,
        permission_mode=run.permission_mode, hard_budget=run.hard_budget_snapshot,
    )
    turn = AgentTurn(
        id=uuid7(), runner_id=runner.id, work_item_id=work.id, run_id=run.id,
        agent_version_id=runner.agent_version_id, capability_snapshot_id=resolved.capability.id,
        turn_no=1, status="started", input_hash="b" * 64,
    )
    db.add(turn)
    await db.flush()
    context = RuntimeContext(
        tenant_id=run.tenant_id, account_id=run.account_id, session_id=run.session_id,
        runner_id=runner.id, user_id=run.user_id, agent_id=runner.agent_id,
        permission_mode=run.permission_mode,
        group_session_id=runner.principal_snapshot.get("group_session_id"),
        group_run_id=runner.principal_snapshot.get("group_run_id"),
        group_membership_id=runner.principal_snapshot.get("group_membership_id"),
    )
    return SimpleNamespace(
        runner=runner, run=run, work=work, turn=turn, context=context, resolved=resolved,
        dispatcher=ActionDispatcher(list(resolved.tool_definitions)),
    )


async def root_probe(db, fixture, agent, *, mode="chat"):
    session = UserSession(
        id=uuid7(), tenant_id=fixture.tenant_id, account_id=fixture.account_id, user_id=fixture.user_id,
        entry_agent_id=agent.agent.id, permission_mode="direct", interaction_mode=mode,
    )
    db.add(session)
    await db.flush()
    run = Run(
        id=uuid7(), tenant_id=fixture.tenant_id, account_id=fixture.account_id,
        session_id=session.id, user_id=fixture.user_id, mode=mode, status="active",
        permission_mode="direct", hard_budget_snapshot=default_run_budget(mode),
    )
    db.add(run)
    await db.flush()
    work = WorkItem(
        id=uuid7(), tenant_id=fixture.tenant_id, run_id=run.id, session_id=session.id, user_id=fixture.user_id,
        mode=mode, owner_agent_id=agent.agent.id, agent_version_id=agent.version.id,
        goal="Verify collaboration", status="running",
    )
    runner = DurableRunner(
        id=uuid7(), tenant_id=fixture.tenant_id, work_item_id=work.id, run_id=run.id,
        session_id=session.id, agent_id=agent.agent.id, agent_version_id=agent.version.id,
        execution_epoch=1, status="running", principal_snapshot={
            "tenant_id": fixture.tenant_id, "account_id": fixture.account_id, "user_id": fixture.user_id,
            "mode": mode, "permission_mode": "direct", "permission_revision": 1, "delegation_chain": [],
        },
    )
    await persist_new_work_runner(db, work, runner)
    run.root_work_item_id = work.id
    await db.flush()
    return await runtime_probe(db, runner=runner)


async def call(db, probe, name, arguments, *, call_id):
    return await probe.dispatcher.dispatch(
        ToolCallItem(call_id=call_id, name=name, arguments=arguments, index=0),
        capability_snapshot=probe.resolved.capability, runtime_context=probe.context,
        db=db, run_id=probe.run.id, work_item_id=probe.work.id, source_turn_id=probe.turn.id,
    )


async def invoke(db, probe, target, *, call_id):
    result = await call(db, probe, "invoke_agent", {"agent_id": target, "task": "Review evidence"}, call_id=call_id)
    return result


async def invoked_probe(db, result):
    assert result.result.success, result.result.model_dump()
    invocation = await db.get(AgentInvocation, result.result.data["agent_invocation_id"])
    work = await db.get(WorkItem, invocation.target_work_item_id)
    runner = await db.get(DurableRunner, work.active_runner_id)
    return await runtime_probe(db, runner=runner), invocation


async def assert_frozen_invocations(db, run_ids):
    for invocation in await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id.in_(run_ids))):
        frozen = await db.get(RunnerToolSnapshot, (invocation.runner_id, invocation.platform_tool_id))
        runner = await db.get(DurableRunner, invocation.runner_id)
        binding = await db.get(AgentVersionTool, (runner.agent_version_id, invocation.platform_tool_id))
        assert invocation.tool_version_id == frozen.tool_version_id == binding.tool_version_id
        assert invocation.tool_version_content_hash == frozen.tool_version_content_hash == binding.tool_version_content_hash
    assert await db.scalar(select(func.count()).select_from(ProviderAttempt).join(
        AgentTurn, AgentTurn.id == ProviderAttempt.agent_turn_id,
    ).where(AgentTurn.run_id.in_(run_ids))) == 0
    assert await db.scalar(text("SELECT @@foreign_key_checks")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chat", "plan"])
async def test_mysql_bound_multilevel_dispatch_keeps_frozen_edges_and_original_acl(mysql_collaboration, mode):
    f = mysql_collaboration
    async with f.factory() as db:
        c = await publish(db, f, "C")
        private = await publish(db, f, "Other user's private Agent", owner=f.other_account_id)
        b = await publish(db, f, "B", bound=(c.agent.id, private.agent.id))
        a = await publish(db, f, "A", bound=(b.agent.id,))
        await db.commit()
        a_probe = await root_probe(db, f, a, mode=mode)
        assert set(a_probe.resolved.capability.effective_tool_keys) == {"invoke_agent"}
        assert a_probe.resolved.capability.callable_agents_scope["agent_ids"] == [b.agent.id]
        assert not a_probe.resolved.capability.discoverable_agents_scope["agent_ids"]
        frozen_a_hash = a.version.content_hash
        # Publishing a different live A does not rewrite this Runner's edges or
        # promote its frozen group-disabled version into network collaboration.
        draft = await update_agent_draft(
            db, agent_id=a.agent.id, expected_draft_revision=a.draft.draft_revision,
            values={"group_enabled": True},
            selection=AssemblySelection(tool_ids=f.tool_ids, bound_agent_ids=(c.agent.id,)),
        )
        latest_a = await publish_agent(
            db, agent_id=a.agent.id, expected_draft_revision=draft.draft_revision,
            actor_account_id=f.account_id, idempotency_record_id=uuid7(),
        )
        assert latest_a.id != a_probe.runner.agent_version_id
        assert a.version.content_hash == frozen_a_hash
        assert await db.get(AgentVersionBinding, (a.version.id, b.agent.id)) is not None
        assert await db.get(AgentVersionBinding, (a.version.id, c.agent.id)) is None
        ab = await invoke(db, a_probe, b.agent.id, call_id="a-b")
        b_probe, ab_row = await invoked_probe(db, ab)
        bc = await invoke(db, b_probe, c.agent.id, call_id="b-c")
        c_probe, bc_row = await invoked_probe(db, bc)
        assert ab.durable_wait and bc.durable_wait
        assert ab.wait_reason == bc.wait_reason == "agent"
        assert ab_row.authorization_snapshot["collaboration_path"] == "bound_agent"
        assert bc_row.authorization_snapshot["collaboration_path"] == "bound_agent"
        assert bc_row.run_id == ab_row.run_id == a_probe.run.id
        assert c_probe.runner.principal_snapshot["delegation_chain"] == [a.agent.id, b.agent.id]
        assert c_probe.runner.principal_snapshot["account_id"] == f.account_id
        assert b_probe.resolved.capability.callable_agents_scope["agent_ids"] == [c.agent.id]
        shortcut = await invoke(db, a_probe, c.agent.id, call_id="a-c-not-bound")
        assert shortcut.result.error.code == "agent_invocation_target_not_bound"
        denied_private = await invoke(db, b_probe, private.agent.id, call_id="b-private")
        assert denied_private.result.error.code == "agent_invocation_rejected"
        assert await db.scalar(select(func.count()).select_from(WorkItem).where(
            WorkItem.run_id == a_probe.run.id, WorkItem.owner_agent_id == private.agent.id,
        )) == 0
        assert await db.scalar(select(func.count()).select_from(WorkItem).where(
            WorkItem.parent_work_item_id == a_probe.work.id, WorkItem.owner_agent_id == c.agent.id,
        )) == 0
        replay = await invoke(db, a_probe, b.agent.id, call_id="a-b")
        assert replay.invocation_id == ab.invocation_id and replay.durable_wait
        await assert_frozen_invocations(db, [a_probe.run.id])
        await db.commit()
        with pytest.raises(DBAPIError, match="immutable"):
            async with db.begin_nested():
                await db.execute(update(AgentVersionBinding).where(
                    AgentVersionBinding.agent_version_id == a.version.id,
                    AgentVersionBinding.bound_agent_id == b.agent.id,
                ).values(bound_agent_id=c.agent.id))


@pytest.mark.asyncio
async def test_mysql_opted_in_network_discovers_forms_group_and_dispatches_peers(mysql_collaboration):
    f = mysql_collaboration
    async with f.factory() as db:
        a, b, c = [await publish(db, f, name, network=True) for name in ("A", "B", "C")]
        disabled = await publish(db, f, "Network disabled", network=False)
        await db.commit()
        versions = [item.version.id for item in (a, b, c, disabled)]
        assert await db.scalar(select(func.count()).select_from(AgentVersionBinding).where(
            AgentVersionBinding.agent_version_id.in_(versions),
        )) == 0
        disabled_probe = await root_probe(db, f, disabled)
        for name, arguments in (
            ("discover_agents", {}),
            ("select_group_members", {"selected_agent_ids": [b.agent.id], "rationale": "Denied"}),
        ):
            rejected = await call(db, disabled_probe, name, arguments, call_id=f"disabled-{name}")
            assert rejected.result.error.code == "tool_not_in_capability_snapshot"
        disabled_candidates = await build_group_emergence_candidate_snapshot(
            db, tenant_id=f.tenant_id, account_id=f.account_id,
            current_agent_id=disabled.agent.id, current_agent_version_id=disabled.version.id,
        )
        assert disabled_candidates["status"] == "unavailable"
        assert disabled_candidates["reason"] == "group_initiator_not_network_enabled"
        probe = await root_probe(db, f, a)
        discovered = await call(db, probe, "discover_agents", {}, call_id="discover")
        assert discovered.result.success, discovered.result.model_dump()
        assert {item["agent_id"] for item in discovered.result.data["candidates"]} == {b.agent.id, c.agent.id}
        candidates = await build_group_emergence_candidate_snapshot(
            db, tenant_id=f.tenant_id, account_id=f.account_id,
            current_agent_id=a.agent.id, current_agent_version_id=a.version.id,
        )
        assert candidates["status"] == "ready"
        assert {item["agent_id"] for item in candidates["candidates"]} == {a.agent.id, b.agent.id, c.agent.id}
        probe.runner.input_snapshot = {"group_emergence": candidates}
        invalid_selection = await call(db, probe, "select_group_members", {
            "selected_agent_ids": [disabled.agent.id], "rationale": "Excluded opt-out",
        }, call_id="select-disabled")
        assert invalid_selection.result.error.code == "group_candidate_not_available"
        selected = await call(db, probe, "select_group_members", {
            "selected_agent_ids": [b.agent.id, c.agent.id], "rationale": "B reviews and C verifies; retain A as anchor",
        }, call_id="select-group")
        assert selected.result.success, selected.result.model_dump()
        assert set(selected.result.data["selected_agent_ids"]) == {a.agent.id, b.agent.id, c.agent.id}
        assert await emerge_group_after_chat(db, source_run_id=probe.run.id, source_runner_id=probe.runner.id) is None
        source_run_id, source_runner_id = probe.run.id, probe.runner.id
        peer_ids = {item.agent.id for item in (a, b, c)}
        a_id, b_id, c_id, disabled_id = a.agent.id, b.agent.id, c.agent.id, disabled.agent.id
        for role, content in (("user", "Review a release"), ("assistant", "Initial assessment is ready")):
            message = Message(
                id=uuid7(), session_id=probe.run.session_id, user_id=f.user_id,
                scope_type="chat", sender_type="user" if role == "user" else "agent",
                sender_user_id=f.user_id if role == "user" else None,
                sender_agent_id=a_id if role == "assistant" else None,
                recipient_type="session", role=role, content=content, status="committed",
                run_id=probe.run.id, work_item_id=probe.work.id,
            )
            db.add(message)
            await db.flush()
            if role == "assistant":
                probe.runner.result_message_id = message.id
        probe.run.status = "completed"
        probe.run.completed_at = utcnow()
        probe.run.terminal_reason = "completed"
        probe.work.status = "succeeded"
        probe.work.active_runner_id = None
        probe.runner.status = "succeeded"
        probe.runner.ended_at = utcnow()
        probe.turn.status = "completed"
        probe.turn.ended_at = utcnow()
        event = await ExecutionEventWriter(db).append(
            session_id=probe.run.session_id, run_id=probe.run.id, event_type="run.completed",
            event_dedupe_key=f"chat-completed:{probe.run.id}", actor_type="service", actor_id="integration",
            entity_type="run", entity_id=probe.run.id, visibility="user", summary="Chat completed",
            payload={}, correlation_id=probe.run.id,
        )
        await enqueue_run_terminal_followups(db, run=probe.run, runner=probe.runner, completion_event=event)
        assert await db.scalar(select(OutboxCommand.id).where(
            OutboxCommand.aggregate_id == probe.run.id, OutboxCommand.action == "emerge_group_after_chat",
        )) is not None
        await db.commit()

    # Group formation consumes committed Chat selection/state in a fresh DB
    # transaction and is idempotent; no existing membership was fabricated.
    async with f.factory() as db:
        emerged = await emerge_group_after_chat(db, source_run_id=source_run_id, source_runner_id=source_runner_id)
        assert emerged is not None
        group, group_run = emerged
        repeated = await emerge_group_after_chat(db, source_run_id=source_run_id, source_runner_id=source_runner_id)
        assert repeated[0].id == group.id and repeated[1].id == group_run.id
        members = list(await db.scalars(select(GroupMembership).where(GroupMembership.group_session_id == group.id)))
        assert {member.agent_id for member in members} == peer_ids
        assert all(member.status == "joined" and member.ended_at is None for member in members)
        assert group.initiator_agent_id == group_run.reporter_agent_id == a_id
        frozen_members = list(await db.scalars(select(GroupRunMember).where(GroupRunMember.group_run_id == group_run.id)))
        assert {member.agent_id for member in frozen_members} == peer_ids
        run = await db.get(Run, group_run.run_id)
        root_work = await db.get(WorkItem, run.root_work_item_id)
        root_runner = await db.get(DurableRunner, root_work.active_runner_id)
        group_probe = await runtime_probe(db, runner=root_runner)
        assert set(group_probe.resolved.capability.callable_agents_scope["agent_ids"]) == {b_id, c_id}
        ab = await invoke(db, group_probe, b_id, call_id="group-a-b")
        b_probe, ab_row = await invoked_probe(db, ab)
        bc = await invoke(db, b_probe, c_id, call_id="group-b-c")
        _, bc_row = await invoked_probe(db, bc)
        assert not ab.durable_wait and not bc.durable_wait
        assert ab_row.authorization_snapshot["collaboration_path"] == "group_membership"
        assert bc_row.authorization_snapshot["collaboration_path"] == "group_membership"
        assert ab_row.group_run_id == bc_row.group_run_id == group_run.id
        assert ab_row.return_route == bc_row.return_route == "group"
        outside = await invoke(db, group_probe, disabled_id, call_id="group-outside")
        assert outside.result.error.code == "membership_policy_fixed_for_run"
        await assert_frozen_invocations(db, [source_run_id, run.id])
        await db.commit()
