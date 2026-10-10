"""Real MySQL capability boundaries; all provider output is generated locally.

Agent publication, frozen bindings, tool dispatch, resume graphs, foreign keys,
immutable triggers and ProviderAttempt records use production code. Knowledge
retrieval is replaced at its detached I/O boundary so no external database or
provider can be contacted by this test.
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

from app.core.model_hub.base import BaseLLM, LLMChunk, TokenUsage
from app.core.tool_registry.registry import ToolRegistry
from app.models.agent_v1 import AgentVersionTool
from app.models.common import utcnow, uuid7
from app.models.foundation import UserSession
from app.models.registry import ToolDefinition, ToolGovernance, ToolVersion
from app.models.runtime_v1 import (
    AgentTurn, CapabilitySnapshot, DurableRunner, ProviderAttempt, Run,
    RunnerToolSnapshot, ToolInvocation, WorkItem,
)
from app.persistence.reliable_writes import ExecutionEventWriter
from app.persistence.runtime_graph import persist_new_work_runner
from app.runtime.agent_loop_coordinator import AgentLoopCoordinator, LoopIdentity
from app.runtime.context_assembler import ContextPackage
from app.runtime.model_adapter import ModelAdapter
from app.runtime.tool_presentation import resolve_tool_presentation
from app.services.run_reliability_service import default_run_budget
from app.services.runtime_resume_service import queue_work_item_resume, tool_continuation
from test.integration.test_agent_collaboration_mysql import (
    call, mysql_collaboration, publish, root_probe, runtime_probe,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def capability_world(mysql_collaboration):
    base = mysql_collaboration
    async with base.factory() as db:
        catalog = {row.tool_key: row for row in await db.scalars(select(ToolDefinition).where(
            ToolDefinition.tool_key.in_(["search_tools", "knowledge_search", "schedule_task", "web_search"]),
        ))}
        keys = ("search_tools", "knowledge_search", "schedule_task")
        for key in keys:
            governance = await db.get(ToolGovernance, catalog[key].id)
            governance.enabled = True
            governance.confirmation_policy = "never"
            # Production schedule rules still require approval for mutations;
            # scheduled execution must reject those mutations before approval.
            if key != "schedule_task":
                governance.risk_rules = []
        await db.commit()
    return SimpleNamespace(**{
        **vars(base), "tool_ids": tuple(catalog[key].id for key in keys), "catalog": catalog,
    })


class LocalCaptureProvider(BaseLLM):
    """Exercises ModelAdapter and durable attempt recording without a network."""
    def __init__(self):
        self.requests = []

    async def chat(self, messages, tools, params):
        raise AssertionError("This integration uses only local streaming")

    async def count_tokens(self, messages, tools=None, params=None):
        raise AssertionError("No remote token counter is allowed")

    async def stream(self, messages, tools, params):
        self.requests.append({"messages": deepcopy(messages), "tools": deepcopy(tools or [])})
        yield LLMChunk(type="text_delta", content="Local capability verification complete")
        yield LLMChunk(
            type="done", stop_reason="stop", model="no-provider-fixture",
            provider_request_id="local-only-capability-test", usage=TokenUsage(input_tokens=1, output_tokens=1),
        )


async def capture_provider_view(db, probe, *, definitions=None):
    provider = LocalCaptureProvider()
    result = await AgentLoopCoordinator(ModelAdapter(provider), probe.dispatcher).run(
        context=ContextPackage(({"role": "user", "content": "Verify capabilities"},), (), 1),
        tool_definitions=list(definitions if definitions is not None else probe.resolved.tool_definitions),
        capability_snapshot=probe.resolved.capability, runtime_context=probe.context,
        model_parameters={}, db=db,
        identity=LoopIdentity(
            probe.runner.id, probe.work.id, probe.run.id, probe.runner.agent_version_id,
            probe.resolved.model_version.id, "openai", execution_epoch=probe.runner.execution_epoch,
        ),
    )
    assert result.status == "completed" and len(provider.requests) == 1
    await db.commit()
    return provider.requests[0]


async def assert_frozen_runtime(db, run_id):
    assert await db.scalar(text("SELECT @@foreign_key_checks")) == 1
    for invocation in await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run_id)):
        frozen = await db.get(RunnerToolSnapshot, (invocation.runner_id, invocation.platform_tool_id))
        runner = await db.get(DurableRunner, invocation.runner_id)
        binding = await db.get(AgentVersionTool, (runner.agent_version_id, invocation.platform_tool_id))
        assert invocation.tool_version_id == frozen.tool_version_id == binding.tool_version_id
        assert invocation.tool_version_content_hash == frozen.tool_version_content_hash == binding.tool_version_content_hash
    attempts = list(await db.scalars(select(ProviderAttempt).join(
        AgentTurn, ProviderAttempt.agent_turn_id == AgentTurn.id,
    ).where(AgentTurn.run_id == run_id)))
    assert all(attempt.status == "succeeded" and attempt.provider_request_id == "local-only-capability-test" for attempt in attempts)


async def scheduled_probe(db, world, agent):
    """Create the normal FK graph with a scheduled principal before resolution."""
    session = UserSession(
        id=uuid7(), tenant_id=world.tenant_id, account_id=world.account_id, user_id=world.user_id,
        entry_agent_id=agent.agent.id, permission_mode="readonly", interaction_mode="chat",
    )
    db.add(session)
    await db.flush()
    run = Run(
        id=uuid7(), tenant_id=world.tenant_id, account_id=world.account_id, session_id=session.id,
        user_id=world.user_id, mode="chat", status="active", permission_mode="readonly",
        hard_budget_snapshot=default_run_budget("chat"),
    )
    db.add(run)
    await db.flush()
    work = WorkItem(
        id=uuid7(), tenant_id=world.tenant_id, run_id=run.id, session_id=session.id, user_id=world.user_id,
        mode="chat", owner_agent_id=agent.agent.id, agent_version_id=agent.version.id,
        goal="List schedules", status="running",
    )
    runner = DurableRunner(
        id=uuid7(), tenant_id=world.tenant_id, work_item_id=work.id, run_id=run.id,
        session_id=session.id, agent_id=agent.agent.id, agent_version_id=agent.version.id,
        execution_epoch=1, status="running", principal_snapshot={
            "tenant_id": world.tenant_id, "account_id": world.account_id, "user_id": world.user_id,
            "mode": "chat", "permission_mode": "readonly", "permission_revision": 1,
            "authentication_source": "schedule", "delegation_chain": [],
        },
    )
    await persist_new_work_runner(db, work, runner)
    run.root_work_item_id = work.id
    await db.flush()
    probe = await runtime_probe(db, runner=runner)
    probe.context = probe.context.model_copy(update={"authentication_source": "schedule"})
    return probe


def assert_list_only(definition):
    assert definition["name"] == "schedule_task"
    assert definition["input_schema"]["properties"]["operation"]["enum"] == ["list"]
    assert set(definition["input_schema"]["properties"]) == {"operation", "status", "limit"}
    assert "operation=list" in definition["description"]


@pytest.mark.asyncio
async def test_mysql_unbound_web_schema_cannot_enter_provider_view(capability_world):
    world = capability_world
    async with world.factory() as db:
        agent = await publish(db, world, "No web binding")
        probe = await root_probe(db, world, agent)
        assert await db.get(AgentVersionTool, (agent.version.id, world.catalog["web_search"].id)) is None
        inventory = await call(db, probe, "search_tools", {}, call_id="inventory")
        assert inventory.result.success
        assert {item["name"] for item in inventory.result.data["tools"]} == {"search_tools", "knowledge_search", "schedule_task"}
        web = await db.get(ToolVersion, world.catalog["web_search"].current_version_id)
        # Even a foreign loaded definition with a real published version must
        # not expand this Agent's capability set at the Provider boundary.
        foreign = {"name": "web_search", "tool_version_id": web.id,
                   "description": web.model_description, "input_schema": web.input_schema}
        view = await capture_provider_view(db, probe, definitions=[*probe.resolved.tool_definitions, foreign])
        assert {item["name"] for item in view["tools"]} == {"search_tools", "schedule_task"}
        assert all(not item["name"].startswith(("web_", "browser_")) for item in view["tools"])
        await assert_frozen_runtime(db, probe.run.id)


@pytest.mark.asyncio
async def test_mysql_schedule_list_contract_survives_search_and_real_resume(capability_world):
    world = capability_world
    async with world.factory() as db:
        agent = await publish(db, world, "Scheduled read only Agent")
        probe = await scheduled_probe(db, world, agent)
        original_hash = agent.version.content_hash
        initial = next(item for item in probe.resolved.tool_definitions if item["name"] == "schedule_task")
        assert_list_only(initial)
        searched = await call(db, probe, "search_tools", {"query": "schedule"}, call_id="search")
        assert searched.result.success
        loaded = next(item for item in searched.result.data["loaded_tool_definitions"] if item["name"] == "schedule_task")
        assert_list_only(loaded)
        view = await capture_provider_view(db, probe)
        assert_list_only(next(item for item in view["tools"] if item["name"] == "schedule_task"))
        for operation in ("create", "update", "pause", "resume", "delete", "run_now"):
            denied = await call(db, probe, "schedule_task", {"operation": operation}, call_id=f"denied-{operation}")
            assert denied.result.error.code == "schedule_recursive_mutation_denied"
            assert denied.invocation_id is None
        listed = await call(db, probe, "schedule_task", {"operation": "list", "limit": 2}, call_id="list")
        assert listed.result.success and listed.durable_wait and listed.wait_reason == "tool"
        invocation = await db.get(ToolInvocation, listed.invocation_id)
        # Execute the real read-only built-in against this isolated database,
        # then provide the same terminal ToolResult consumed by resume service.
        result = await ToolRegistry.execute("schedule_task", invocation.arguments, probe.context.model_copy(update={
            "tool_invocation_id": invocation.id, "tool_version_id": invocation.tool_version_id,
        }), db=db)
        assert result.success and result.data["operation"] == "list" and result.data["count"] == 0
        invocation.status = "succeeded"
        invocation.result_data = result.model_dump(mode="json")
        invocation.version += 1
        probe.runner.input_snapshot = {"loaded_tool_version_ids": [item["tool_version_id"] for item in probe.resolved.tool_definitions]}
        probe.runner.status = "succeeded"
        probe.runner.ended_at = utcnow()
        probe.work.status = "waiting_tool"
        probe.work.wait_reason = "tool"
        probe.work.active_runner_id = None
        probe.run.status = "waiting_tool"
        probe.run.wait_reason = "waiting_tool"
        settled = await ExecutionEventWriter(db).append(
            session_id=probe.run.session_id, run_id=probe.run.id, event_type="tool.succeeded",
            event_dedupe_key=f"integration:{invocation.id}:settled", actor_type="service", actor_id="capability-test",
            entity_type="tool_invocation", entity_id=invocation.id, visibility="user", summary="List completed",
            payload={"tool_key": "schedule_task"}, correlation_id=probe.run.id,
        )
        pending = await tool_continuation(db, work_item_id=probe.work.id, source_turn_id=probe.turn.id)
        for message in pending:
            if message.get("role") == "tool":
                data = json.loads(message["content"])["data"] or {}
                if "loaded_tool_definitions" in data:
                    assert data["loaded_tool_definitions"] == []
                    assert "operation=list" in next(item for item in data["tools"] if item["name"] == "schedule_task")["description"]
        replacement = await queue_work_item_resume(
            db, work=probe.work, run=probe.run, source_runner=probe.runner,
            pending_results=pending, reason="tool_result", caused_by_event_id=settled.id, actor_id="capability-test",
        )
        replacement_id, run_id, version_id = replacement.id, probe.run.id, agent.version.id
        await db.commit()
    async with world.factory() as db:
        replacement = await db.get(DurableRunner, replacement_id)
        replacement.status = "running"
        resumed = await runtime_probe(db, runner=replacement)
        resumed.context = resumed.context.model_copy(update={"authentication_source": "schedule"})
        assert resumed.runner.agent_version_id == version_id and resumed.runner.execution_epoch == 2
        restored = await resolve_tool_presentation(
            db, capability_snapshot=resumed.resolved.capability, authentication_source="schedule",
            tool_definitions=list(resumed.resolved.capability.authorized_tool_versions),
        )
        assert_list_only(next(item for item in restored.tool_definitions if item["name"] == "schedule_task"))
        view = await capture_provider_view(db, resumed, definitions=restored.tool_definitions)
        assert_list_only(next(item for item in view["tools"] if item["name"] == "schedule_task"))
        assert resumed.resolved.assembly.content_hash == original_hash
        await assert_frozen_runtime(db, run_id)


@pytest.mark.asyncio
async def test_mysql_inline_tool_uses_standard_contract_and_honors_live_disable(capability_world, monkeypatch):
    world = capability_world
    retrieved = []
    async def local_knowledge(**kwargs):
        retrieved.append(kwargs)
        return {"query": kwargs["query"], "count": 0, "results": [], "revision_manifest": [], "retrieval_profile": {}}
    monkeypatch.setattr("app.core.tool_registry.builtin.knowledge_search.search_knowledge_detached", local_knowledge)
    tool_id = world.catalog["knowledge_search"].id
    try:
        async with world.factory() as db:
            agent = await publish(db, world, "Inline governed Agent")
            probe = await root_probe(db, world, agent)
            loaded = await call(db, probe, "search_tools", {"query": "knowledge"}, call_id="load")
            assert loaded.result.success
            probe.dispatcher.add_definitions(loaded.result.data["loaded_tool_definitions"])
            invalid = await call(db, probe, "knowledge_search", {}, call_id="invalid")
            assert invalid.result.error.code == "tool_arguments_schema_error" and invalid.invocation_id is None
            allowed = await call(db, probe, "knowledge_search", {"query": "release evidence"}, call_id="allowed")
            assert allowed.result.success and allowed.result.data["count"] == 0
            assert len(retrieved) == 1
            assert retrieved[0]["account_id"] == world.account_id and retrieved[0]["runner_id"] == probe.runner.id
            saved = await db.get(ToolInvocation, allowed.invocation_id)
            assert saved.execution_mode == "inline" and saved.status == "succeeded"
            await db.commit()
        async with world.factory() as db:
            policy = await db.get(ToolGovernance, tool_id)
            policy.enabled = False
            policy.version += 1
            await db.commit()
        async with world.factory() as db:
            # Reuse the original frozen schema/Dispatcher after a committed
            # policy change to prove authorization is checked at execution.
            denied = await call(db, probe, "knowledge_search", {"query": "after disable"}, call_id="disabled")
            assert denied.result.error.code == "platform_tool_disabled" and denied.invocation_id is None
            assert len(retrieved) == 1
            fresh = await call(db, probe, "search_tools", {"query": "knowledge"}, call_id="refresh")
            item = next(item for item in fresh.result.data["tools"] if item["name"] == "knowledge_search")
            assert not item["execution_available"] and item["blocked_reason"] == "platform_tool_disabled"
            assert await db.scalar(select(func.count()).select_from(ToolInvocation).where(
                ToolInvocation.run_id == probe.run.id, ToolInvocation.tool_key == "knowledge_search",
            )) == 1
            await assert_frozen_runtime(db, probe.run.id)
            await db.commit()
    finally:
        async with world.factory() as db:
            policy = await db.get(ToolGovernance, tool_id)
            policy.enabled = True
            policy.version += 1
            await db.commit()
