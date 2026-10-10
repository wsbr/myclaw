"""Migrated MySQL proves catalog upgrades preserve sealed Agent bindings."""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.registries_v1 import patch_platform_tool
from app.config import settings
from app.core.tool_registry.base import RuntimeContext
from app.core.tool_registry.builtin.search_group_history import SearchGroupHistoryTool
from app.core.tool_registry.runtime_tools import RUNTIME_TOOLS, is_runtime_tool_implementation
from app.core.tool_registry.schema import builtin_result_schema
from app.deps import CurrentAccount
from app.models.agent_v1 import AgentVersionTool
from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, UserIdentity, UserSession
from app.models.registry import (
    ConformanceRun, ModelVersion, PolicyDefinition, RegisteredModel,
    SandboxProfile, ToolGovernance, ToolVersion,
)
from app.models.runtime_v1 import (
    AgentTurn, CapabilitySnapshot, Clarification, DurableRunner, PlanUpdate,
    Run, RunnerToolSnapshot, ToolInvocation, WorkItem, WorkingPlan,
)
from app.models.tenant_v1 import Tenant, TenantMembership
from app.runtime.action_dispatcher import ActionDispatcher
from app.runtime.protocol import ToolCallItem
from app.runtime.snapshot_resolver import SnapshotResolver
from app.schemas.registry_v1 import PlatformToolPatch
from app.services.agent_assembly_service import (
    AssemblySelection, create_agent, publish_agent, update_agent_draft,
)
from app.services.registry_reconcile import (
    CodeToolSpec, builtin_tool_specs, reconcile_platform_tools, seed_core_registries,
)


pytestmark = pytest.mark.integration


async def _assert_runtime_dispatch_roundtrip(
    factory, *, tenant_id, user_id, account_id, agent_id, agent_version_id,
):
    """No mocked policies, handlers, FK checks, or append-only guards."""
    session_id, run_id, work_id, runner_id, turn_id = (uuid7() for _ in range(5))
    async with factory() as db:
        db.add(UserSession(
            id=session_id, tenant_id=tenant_id, user_id=user_id, account_id=account_id,
            entry_agent_id=agent_id, permission_mode="plan_readonly", interaction_mode="plan",
        ))
        await db.flush()
        run = Run(
            id=run_id, tenant_id=tenant_id, account_id=account_id,
            session_id=session_id, user_id=user_id, mode="plan", status="active",
            permission_mode="plan_readonly", hard_budget_snapshot={"max_tool_invocations": 3},
        )
        db.add(run)
        await db.flush()
        db.add(WorkItem(
            id=work_id, tenant_id=tenant_id, run_id=run_id, session_id=session_id,
            user_id=user_id, mode="plan", owner_agent_id=agent_id,
            agent_version_id=agent_version_id, goal="Verify explicit runtime bindings", status="running",
        ))
        await db.flush()
        run.root_work_item_id = work_id
        runner = DurableRunner(
            id=runner_id, tenant_id=tenant_id, work_item_id=work_id, run_id=run_id,
            session_id=session_id, agent_id=agent_id, agent_version_id=agent_version_id,
            execution_epoch=1, status="running",
            principal_snapshot={"tenant_id": tenant_id, "account_id": account_id, "mode": "plan"},
        )
        db.add(runner)
        await db.flush()
        resolved = await SnapshotResolver().resolve(
            db, runner=runner, execution_principal=runner.principal_snapshot,
            permission_mode="plan_readonly", hard_budget=run.hard_budget_snapshot,
        )
        capability_id = resolved.capability.id
        db.add(AgentTurn(
            id=turn_id, runner_id=runner_id, work_item_id=work_id, run_id=run_id,
            agent_version_id=agent_version_id, capability_snapshot_id=capability_id,
            turn_no=1, status="started", input_hash="b" * 64,
        ))
        await db.flush()
        context = RuntimeContext(
            tenant_id=tenant_id, account_id=account_id, session_id=session_id,
            runner_id=runner_id, user_id=user_id, agent_id=agent_id,
            permission_mode="plan_readonly",
        )
        calls = [
            ToolCallItem(call_id="catalog", name="search_tools", arguments={}, index=0),
            ToolCallItem(call_id="plan", name="update_plan", arguments={
                "items": [{"text": "Collect evidence", "status": "in_progress"}],
                "expected_revision": 0,
            }, index=1),
            ToolCallItem(call_id="question", name="ask_user", arguments={
                "question": "Which evidence should be prioritized?",
            }, index=2),
        ]
        results = await ActionDispatcher(list(resolved.tool_definitions)).dispatch_batch(
            calls, capability_snapshot=resolved.capability, runtime_context=context,
            db=db, run_id=run_id, work_item_id=work_id, source_turn_id=turn_id,
        )
        assert all(result.result.success for result in results), [result.result.model_dump() for result in results]
        assert all(result.invocation_id for result in results)
        assert results[2].durable_wait and results[2].wait_reason == "clarification"
        clarification_id = results[2].result.data["clarification_id"]
        plan_id = results[1].result.data["working_plan"]["id"]
        invocation_ids = [result.invocation_id for result in results]
        await db.commit()

    # A new database session replays durable outcomes, including a pending user
    # wait. It must neither consume a fourth budget slot nor append a new plan
    # revision/message/Clarification when the Runner is retried.
    async with factory() as db:
        capability = await db.get(CapabilitySnapshot, capability_id)
        dispatcher = ActionDispatcher(list(capability.authorized_tool_versions))
        replayed = await dispatcher.dispatch_batch(
            calls, capability_snapshot=capability, runtime_context=context,
            db=db, run_id=run_id, work_item_id=work_id, source_turn_id=turn_id,
        )
        assert [result.invocation_id for result in replayed] == invocation_ids
        assert all(result.result.success for result in replayed)
        assert replayed[2].durable_wait and replayed[2].wait_reason == "clarification"
        assert replayed[2].result.data["clarification_id"] == clarification_id
        invocations = list(await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run_id)))
        assert len(invocations) == 3
        for invocation in invocations:
            frozen = await db.get(RunnerToolSnapshot, (runner_id, invocation.platform_tool_id))
            assert invocation.tool_version_id == frozen.tool_version_id
            assert invocation.tool_version_content_hash == frozen.tool_version_content_hash
            assert invocation.status == "succeeded" and invocation.execution_mode == "inline"
        question = next(item for item in invocations if item.tool_key == "ask_user")
        assert question.authorization_snapshot["runtime_durable_wait"] is True
        assert question.authorization_snapshot["runtime_wait_reason"] == "clarification"
        assert await db.scalar(select(func.count()).select_from(Clarification).where(Clarification.run_id == run_id)) == 1
        assert (await db.get(Clarification, clarification_id)).status == "open"
        assert (await db.get(WorkingPlan, plan_id)).revision == 1
        assert await db.scalar(select(func.count()).select_from(PlanUpdate).where(PlanUpdate.working_plan_id == plan_id)) == 1
        denied = await dispatcher.dispatch(
            ToolCallItem(call_id="over-budget", name="search_tools", arguments={}, index=0),
            capability_snapshot=capability, runtime_context=context, db=db,
            run_id=run_id, work_item_id=work_id, source_turn_id=turn_id,
        )
        assert denied.result.error.code == "budget_exhausted"
        await db.commit()


@pytest.mark.asyncio
async def test_mysql_runtime_catalog_publish_and_immutable_binding_upgrade():
    url = os.getenv("MYCLAW_V3_ACCEPTANCE_DATABASE_URL")
    if not url:
        pytest.skip("Select an isolated migrated MySQL with MYCLAW_V3_ACCEPTANCE_DATABASE_URL")
    if not (make_url(url).database or "").endswith("_test"):
        raise ValueError("runtime catalog integration requires a disposable *_test database")
    engine = create_async_engine(url, isolation_level="READ COMMITTED")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid7().replace("-", "")
    try:
        async with factory() as db:
            tenant = Tenant(id=uuid7(), slug=f"runtime-catalog-{suffix}", name="Catalog test")
            user = UserIdentity(user_id=f"runtime-catalog-{suffix}")
            db.add_all([tenant, user])
            await db.flush()
            account = PlatformAccount(
                id=uuid7(), tenant_id=tenant.id, user_id=user.user_id, display_name="Catalog builder",
            )
            db.add(account)
            await db.flush()
            db.add(TenantMembership(
                id=uuid7(), tenant_id=tenant.id, account_id=account.id,
                role_key="admin", status="active",
            ))
            await db.flush()
            current = CurrentAccount(account, frozenset({"admin", "builder"}))
            model = RegisteredModel(
                id=uuid7(), key=f"runtime-catalog-{suffix}", provider="openai",
                model_name="catalog-fixture", enabled=True, status="active",
            )
            db.add(model)
            await db.flush()
            model_version = ModelVersion(
                id=uuid7(), model_id=model.id, version_no=1, provider="openai",
                model_name="catalog-fixture", content_hash="a" * 64,
                capabilities={"tool_calling": True},
            )
            db.add(model_version)
            await db.flush()
            model.current_version_id = model_version.id
            # Synthetic conformance is scoped to this isolated fixture model;
            # publication checks are real and no provider request is issued.
            db.add(ConformanceRun(
                id=uuid7(), subject_type="model_version", subject_id=model_version.id,
                suite_version=settings.provider_conformance_suite_version,
                idempotency_key=uuid7(), request_hash="a" * 64, status="passed",
                started_at=utcnow(), ended_at=utcnow(), result_summary={},
            ))
            await seed_core_registries(db)
            policies = {row.policy_key: row for row in await db.scalars(select(PolicyDefinition))}
            sandbox = await db.scalar(select(SandboxProfile).where(SandboxProfile.profile_key == "local"))
            specs = builtin_tool_specs()
            legacy = CodeToolSpec(
                tool_key="search_group_history", display_name="Search Group History",
                display_name_zh="搜索群组历史", description=SearchGroupHistoryTool.description,
                input_schema=SearchGroupHistoryTool.input_schema,
                output_schema=builtin_result_schema("search_group_history"),
                implementation_ref="app.core.tool_registry.builtin.search_group_history:SearchGroupHistoryTool",
            )
            old_catalog = await reconcile_platform_tools(
                db, [spec for spec in specs if spec.tool_key != legacy.tool_key] + [legacy],
                job_run_id=f"legacy:{suffix}",
            )
            by_key = {row.tool_key: row for row in old_catalog}
            for name in RUNTIME_TOOLS:
                governance = await db.get(ToolGovernance, by_key[name].id)
                governance.enabled = True
                governance.confirmation_policy = "never"
                governance.risk_rules = []
            history = by_key["search_group_history"]
            definition_id, old_tool_version_id = history.id, history.current_version_id
            old_tool_version = await db.get(ToolVersion, old_tool_version_id)
            old_tool_hash = old_tool_version.content_hash
            agent, draft = await create_agent(
                db, tenant_id=tenant.id, owner_account_id=account.id,
                created_by_account_id=account.id, name="Catalog Agent",
                description="Explicit runtime tool binding test",
                initial_values={
                    "instructions": "Use only explicitly authorized tools.",
                    "model_id": model.id, "sandbox_profile_id": sandbox.id,
                    "context_policy_id": policies["context.standard"].id,
                    "history_policy_id": policies["history.recent"].id,
                    "memory_write_policy_id": policies["memory_write.none"].id,
                    "memory_recall_policy_id": policies["memory_recall.none"].id,
                },
                selection=AssemblySelection(tool_ids=(history.id,)),
            )
            published_old = await publish_agent(
                db, agent_id=agent.id, expected_draft_revision=draft.draft_revision,
                actor_account_id=account.id, idempotency_record_id=uuid7(),
            )
            old_assembly_hash = published_old.content_hash
            await db.commit()

            new_catalog = await reconcile_platform_tools(db, specs, job_run_id=f"upgrade:{suffix}")
            by_key = {row.tool_key: row for row in new_catalog}
            version_count = await db.scalar(select(func.count()).select_from(ToolVersion))
            await reconcile_platform_tools(db, specs, job_run_id=f"replay:{suffix}")
            assert await db.scalar(select(func.count()).select_from(ToolVersion)) == version_count
            assert by_key["search_group_history"].id == definition_id
            assert by_key["search_group_history"].current_version_id != old_tool_version_id
            old_binding = await db.get(AgentVersionTool, (published_old.id, definition_id))
            assert old_binding.tool_version_id == old_tool_version_id
            assert old_binding.tool_version_content_hash == old_tool_hash
            assert old_tool_version.implementation_ref == legacy.implementation_ref
            assert published_old.content_hash == old_assembly_hash
            assert await db.scalar(select(func.count()).select_from(AgentVersionTool).where(
                AgentVersionTool.agent_version_id == published_old.id,
            )) == 1  # Reconciliation grants no new capabilities to old Agents.

            draft = await update_agent_draft(
                db, agent_id=agent.id, expected_draft_revision=draft.draft_revision,
                values={}, selection=AssemblySelection(
                    tool_ids=tuple(by_key[name].id for name in RUNTIME_TOOLS),
                ),
            )
            published_new = await publish_agent(
                db, agent_id=agent.id, expected_draft_revision=draft.draft_revision,
                actor_account_id=account.id, idempotency_record_id=uuid7(),
            )
            bindings = list(await db.scalars(select(AgentVersionTool).where(
                AgentVersionTool.agent_version_id == published_new.id,
            )))
            assert len(bindings) == len(RUNTIME_TOOLS) == 11
            names_by_id = {row.id: row.tool_key for row in new_catalog}
            for binding in bindings:
                version = await db.get(ToolVersion, binding.tool_version_id)
                assert is_runtime_tool_implementation(names_by_id[binding.platform_tool_id], version.implementation_ref)
                assert binding.tool_version_content_hash == version.content_hash
            await db.commit()

            await _assert_runtime_dispatch_roundtrip(
                factory, tenant_id=tenant.id, user_id=user.user_id, account_id=account.id,
                agent_id=agent.id, agent_version_id=published_new.id,
            )

            # The migrated MySQL guard itself rejects attempts to rewrite the
            # previously sealed binding; foreign-key checks stay enabled.
            with pytest.raises(DBAPIError, match="immutable"):
                async with db.begin_nested():
                    await db.execute(update(AgentVersionTool).where(
                        AgentVersionTool.agent_version_id == published_old.id,
                        AgentVersionTool.platform_tool_id == definition_id,
                    ).values(tool_version_id=by_key["search_group_history"].current_version_id))

            runtime_tool = by_key["ask_user"]
            governance = await db.get(ToolGovernance, runtime_tool.id)
            for changes in (
                {"confirmation_policy": "per_call"},
                {"risk_rules": [{"when": {"argument": "question"}, "confirmation": "per_call"}]},
            ):
                with pytest.raises(HTTPException) as rejected:
                    async with db.begin_nested():
                        await patch_platform_tool(
                            runtime_tool.id, PlatformToolPatch(**changes),
                            if_match=str(governance.version), idempotency_key=uuid7(), db=db, current=current,
                        )
                assert rejected.value.status_code == 422
                assert rejected.value.detail["code"] == "runtime_confirmation_policy_unsupported"
            ordinary = by_key["write_file"]
            ordinary_governance = await db.get(ToolGovernance, ordinary.id)
            response = await patch_platform_tool(
                ordinary.id, PlatformToolPatch(confirmation_policy="per_call"),
                if_match=str(ordinary_governance.version), idempotency_key=uuid7(), db=db, current=current,
            )
            assert response["confirmation_policy"] == "per_call"
    finally:
        await engine.dispose()
