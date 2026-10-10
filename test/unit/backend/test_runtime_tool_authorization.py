"""Explicit frozen runtime bindings, execution policy, and durable replay."""

from copy import deepcopy
from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.tool_registry.base import RuntimeContext, ToolResult
from app.core.tool_registry.runtime_tools import RUNTIME_TOOLS
from app.database import Base
from app.models.agent_v1 import AgentAssemblyVersion, AgentVersionTool
from app.models.foundation import UserSession
from app.models.registry import ModelVersion, ToolDefinition, ToolGovernance, ToolVersion
from app.models.runtime_v1 import DurableRunner, Run, RunnerToolSnapshot, ToolInvocation, WorkItem
from app.runtime.action_dispatcher import ActionDispatcher
from app.runtime.protocol import ToolCallItem
from app.runtime.snapshot_resolver import SnapshotResolver

pytestmark = pytest.mark.unit


@pytest.fixture
async def runtime_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async def empty(*args, **kwargs):
        return []
    async def no_policies(*args, **kwargs):
        return {}
    monkeypatch.setattr("app.runtime.snapshot_resolver.resolve_frozen_runtime_policies", no_policies)
    monkeypatch.setattr("app.runtime.snapshot_resolver.discover_agent_cards", empty)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        now = datetime.utcnow()
        db.add_all([
            UserSession(id="session", tenant_id="tenant", user_id="user", account_id="account", entry_agent_id="agent"),
            ModelVersion(id="model-version", model_id="model", version_no=1, provider="openai", model_name="test", default_parameters={}, capabilities={}, adapter_protocol="agent_native.v2", content_hash="m" * 64, published_at=now),
            AgentAssemblyVersion(id="assembly", agent_id="agent", version_no=1, publication_status="published", name="Agent", description="", instructions="", model_id="model", model_version_id="model-version", group_enabled=False, discovery_config={}, collaboration_config={}, created_by_account_id="account", immutable_at=now),
            Run(id="run", tenant_id="tenant", account_id="account", session_id="session", user_id="user", mode="chat", status="active"),
            WorkItem(id="work", tenant_id="tenant", run_id="run", session_id="session", user_id="user", mode="chat", owner_agent_id="agent", agent_version_id="assembly", goal="test", status="running"),
            DurableRunner(id="runner", tenant_id="tenant", work_item_id="work", run_id="run", session_id="session", agent_id="agent", agent_version_id="assembly", execution_epoch=1, status="running"),
        ])
        await db.commit()
        yield db
    await engine.dispose()


async def bind(db, name, *, enabled=True, confirmation="never", legacy=False, eager=None):
    contract = deepcopy(RUNTIME_TOOLS.get(name, {
        "description": "test", "input_schema": {"type": "object"}, "output_schema": {},
        "implementation_ref": "test:probe", "execution_mode": "inline",
        "side_effect": "none", "annotations": {}, "registration_hash": "b" * 64,
    }))
    if legacy:
        contract["implementation_ref"] = "app.core.tool_registry.builtin.search_group_history:SearchGroupHistoryTool"
    if eager is not None:
        contract["annotations"]["eager"] = eager
    order = int(await db.scalar(select(func.count()).select_from(AgentVersionTool)) or 0)
    tool = ToolDefinition(id=f"tool-{name}", tool_key=name, namespace="runtime", origin="builtin", display_name=name, current_version_id=f"version-{name}")
    version = ToolVersion(id=f"version-{name}", definition_id=tool.id, version_no=1, model_description=contract["description"], input_schema=contract["input_schema"], output_schema=contract["output_schema"], annotations=contract["annotations"], implementation_ref=contract["implementation_ref"], implementation_version="test-v1", execution_mode=contract["execution_mode"], side_effect=contract["side_effect"], content_hash=contract["registration_hash"])
    db.add_all([tool, version, ToolGovernance(tool_definition_id=tool.id, enabled=enabled, confirmation_policy=confirmation), AgentVersionTool(agent_version_id="assembly", platform_tool_id=tool.id, tool_version_id=version.id, tool_version_content_hash=version.content_hash, sort_order=order)])
    await db.flush()
    return tool, version


async def resolve(db, *, permission="direct", mode="chat", budget=None):
    runner = await db.get(DurableRunner, "runner")
    run = await db.get(Run, "run")
    run.mode = mode
    run.permission_mode = permission
    resolved = await SnapshotResolver().resolve(db, runner=runner, execution_principal={"tenant_id": "tenant", "account_id": "account", "mode": mode}, permission_mode=permission, hard_budget=budget)
    await db.flush()
    return resolved


def context(permission="direct"):
    return RuntimeContext(tenant_id="tenant", account_id="account", session_id="session", runner_id="runner", user_id="user", agent_id="agent", permission_mode=permission)


async def dispatch(db, resolved, name, *, arguments=None, call_id="call", definition=None, executor=None):
    kwargs = {"executor": executor} if executor else {}
    dispatcher = ActionDispatcher([definition] if definition else list(resolved.tool_definitions), **kwargs)
    return await dispatcher.dispatch(ToolCallItem(call_id=call_id, name=name, arguments=arguments or {}, index=0), capability_snapshot=resolved.capability, runtime_context=context(resolved.capability.permission_mode), db=db, run_id="run", work_item_id="work", source_turn_id="turn")


async def test_empty_immutable_assembly_gets_no_implicit_runtime_tools(runtime_db):
    resolved = await resolve(runtime_db)
    assert resolved.tool_definitions == ()
    assert resolved.capability.assembled_tool_keys == []
    assert resolved.capability.authorized_tool_versions == []
    assert await runtime_db.scalar(select(func.count()).select_from(AgentVersionTool)) == 0


async def test_explicit_runtime_binding_is_frozen_and_old_versions_are_not_upgraded(runtime_db):
    tool, version = await bind(runtime_db, "search_tools")
    tool.current_version_id = "a-newer-unbound-version"
    resolved = await resolve(runtime_db)
    assert resolved.capability.effective_tool_keys == ["search_tools"]
    definition, = resolved.tool_definitions
    assert definition["source"] == "runtime"
    assert definition["tool_version_id"] == version.id
    frozen = await runtime_db.get(RunnerToolSnapshot, {"runner_id": "runner", "platform_tool_id": tool.id})
    assert frozen.tool_version_id == version.id
    assert frozen.tool_version_content_hash == version.content_hash


@pytest.mark.parametrize("bound_search,search_enabled,expected", [
    (False, True, {"rare_probe"}), (True, False, {"rare_probe"}),
    (True, True, {"search_tools"}),
])
async def test_non_eager_tools_remain_visible_without_effective_search(runtime_db, bound_search, search_enabled, expected):
    await bind(runtime_db, "rare_probe", eager=False)
    if bound_search:
        await bind(runtime_db, "search_tools", enabled=search_enabled)
    resolved = await resolve(runtime_db)
    assert {item["name"] for item in resolved.tool_definitions} == expected
    assert "rare_probe" in resolved.capability.effective_tool_keys


@pytest.mark.parametrize("name,permission,mode,enabled,confirmation,budget,reason", [
    ("search_tools", "direct", "chat", False, "never", None, "platform_tool_disabled"),
    ("ask_user", "readonly", "chat", True, "never", None, "permission_mode_readonly"),
    ("invoke_agent", "plan_readonly", "plan", True, "never", None, "permission_mode_readonly"),
    ("send_group_message", "direct", "chat", True, "never", None, "runtime_tool_mode_denied"),
    ("select_group_members", "direct", "plan", True, "never", None, "runtime_tool_mode_denied"),
    ("search_tools", "direct", "chat", True, "per_call", None, "runtime_confirmation_policy_unsupported"),
    ("search_tools", "direct", "chat", True, "never", {"tool_calls_remaining": 0}, "hard_budget_exhausted"),
])
async def test_runtime_snapshot_applies_governance_permissions_mode_and_budget(runtime_db, name, permission, mode, enabled, confirmation, budget, reason):
    await bind(runtime_db, name, enabled=enabled, confirmation=confirmation)
    resolved = await resolve(runtime_db, permission=permission, mode=mode, budget=budget)
    assert resolved.capability.effective_tool_keys == []
    assert {item["reason_code"] for item in resolved.capability.denied_capabilities} == {reason}


@pytest.mark.parametrize("name", ["update_plan", "ask_user"])
async def test_only_trusted_planning_tools_are_available_before_plan_approval(runtime_db, name):
    await bind(runtime_db, name)
    resolved = await resolve(runtime_db, permission="plan_readonly", mode="plan")
    assert resolved.capability.effective_tool_keys == [name]
    assert resolved.tool_definitions[0]["side_effect"] != "none"


async def test_runtime_dispatch_uses_durable_invocation_and_counts_tool_budget(runtime_db):
    await bind(runtime_db, "search_tools")
    run = await runtime_db.get(Run, "run")
    run.hard_budget_snapshot = {"max_tool_invocations": 1}
    resolved = await resolve(runtime_db)
    first = await dispatch(runtime_db, resolved, "search_tools")
    assert first.result.success
    assert first.invocation_id
    saved = await runtime_db.get(ToolInvocation, first.invocation_id)
    assert saved.tool_version_id == "version-search_tools"
    assert saved.status == "succeeded"
    second = await dispatch(runtime_db, resolved, "search_tools", call_id="next")
    assert second.result.error.code == "budget_exhausted"
    replay = await dispatch(runtime_db, resolved, "search_tools")
    assert replay.result.success
    assert replay.invocation_id == first.invocation_id
    assert await runtime_db.scalar(select(func.count()).select_from(ToolInvocation)) == 1


@pytest.mark.parametrize("name,reason,arguments", [
    ("ask_user", "clarification", {"question": "Choose?"}),
    ("invoke_agent", "agent", {"agent_id": "peer", "task": "Review"}),
])
async def test_runtime_idempotent_replay_retains_wait_and_never_reexecutes(runtime_db, monkeypatch, name, reason, arguments):
    await bind(runtime_db, name)
    resolved = await resolve(runtime_db)
    seen = []
    async def coordinate(*args, **kwargs):
        seen.append(args[0])
        data = {"clarification_id": "clarification", "status": "open"} if name == "ask_user" else {"agent_invocation_id": "invocation", "status": "queued"}
        return ToolResult(status="succeeded", summary="Waiting", data=data), True, reason
    monkeypatch.setattr("app.runtime.action_dispatcher.dispatch_coordination_tool", coordinate)
    first = await dispatch(runtime_db, resolved, name, arguments=arguments)
    replay = await dispatch(runtime_db, resolved, name, arguments=arguments)
    assert first.result.success and replay.result.success
    assert first.durable_wait and replay.durable_wait
    assert first.wait_reason == replay.wait_reason == reason
    assert first.invocation_id == replay.invocation_id
    assert seen == [name]


@pytest.mark.parametrize("change,code", [
    ("disabled", "platform_tool_disabled"),
    ("confirmation", "runtime_confirmation_policy_unsupported"),
    ("risk_rule", "runtime_confirmation_policy_unsupported"),
    ("binding_deleted", "runtime_tool_binding_invalid"),
    ("hash", "runtime_tool_binding_invalid"),
    ("source_spoof", "runtime_tool_identity_mismatch"),
])
async def test_stale_or_forged_runtime_authorization_is_rejected(runtime_db, change, code):
    name = "search_group_history" if change == "source_spoof" else "search_tools"
    tool, version = await bind(runtime_db, name, legacy=change == "source_spoof")
    resolved = await resolve(runtime_db)
    definition = dict(resolved.tool_definitions[0])
    governance = await runtime_db.get(ToolGovernance, tool.id)
    if change == "disabled":
        governance.enabled = False
    elif change == "confirmation":
        governance.confirmation_policy = "per_call"
    elif change == "risk_rule":
        governance.risk_rules = [{"when": "always", "confirmation": "per_call"}]
    elif change == "binding_deleted":
        binding = await runtime_db.get(AgentVersionTool, {"agent_version_id": "assembly", "platform_tool_id": tool.id})
        await runtime_db.delete(binding)
    elif change == "hash":
        definition["tool_version_content_hash"] = "f" * 64
    else:
        definition["source"] = "runtime"
    await runtime_db.flush()
    result = await dispatch(runtime_db, resolved, name, definition=definition, arguments={"query": "test"})
    assert result.result.error.code == code
    assert await runtime_db.scalar(select(func.count()).select_from(ToolInvocation)) == 0


async def test_legacy_bound_same_name_uses_its_platform_executor(runtime_db):
    await bind(runtime_db, "search_group_history", legacy=True)
    resolved = await resolve(runtime_db)
    observed = []
    async def legacy(name, arguments, context, db):
        observed.append(context.tool_version_id)
        return ToolResult(status="succeeded", summary="Legacy contract", data={"hits": [], "scanned_through_sequence": 0, "max_sequence": 0, "next_cursor": None})
    result = await dispatch(runtime_db, resolved, "search_group_history", arguments={"query": "history"}, executor=legacy)
    assert result.result.success
    assert observed == ["version-search_group_history"]
    assert resolved.tool_definitions[0]["source"] == "platform"


async def test_execution_checks_actual_mode_not_forged_annotations(runtime_db):
    await bind(runtime_db, "send_group_message")
    resolved = await resolve(runtime_db, mode="group")
    run = await runtime_db.get(Run, "run")
    run.mode = "chat"
    definition = dict(resolved.tool_definitions[0], annotations={"runtime_modes": ["chat"]})
    result = await dispatch(runtime_db, resolved, "send_group_message", definition=definition, arguments={"content": "hello"})
    assert result.result.error.code == "runtime_tool_mode_denied"


async def test_execution_uses_frozen_side_effect_not_forged_runtime_metadata(runtime_db, monkeypatch):
    await bind(runtime_db, "invoke_agent")
    resolved = await resolve(runtime_db)
    # Model-visible metadata cannot erase the true execution permission policy.
    resolved.capability.permission_mode = "readonly"
    definition = dict(resolved.tool_definitions[0], side_effect="none", annotations={"runtime_plan_readonly_allowed": True})
    result = await dispatch(runtime_db, resolved, "invoke_agent", definition=definition, arguments={"agent_id": "peer", "task": "Review"})
    assert result.result.error.code == "permission_mode_denied"
    assert await runtime_db.scalar(select(func.count()).select_from(ToolInvocation)) == 0


async def test_forged_runtime_source_and_name_cannot_grant_unbound_tool(runtime_db):
    resolved = await resolve(runtime_db)
    forged = {"id": resolved.capability.id, "effective_tool_keys": ["search_tools"]}
    dispatcher = ActionDispatcher([dict(RUNTIME_TOOLS["search_tools"])])
    result = await dispatcher.dispatch(
        ToolCallItem(call_id="call", name="search_tools", arguments={}, index=0),
        capability_snapshot=forged, runtime_context=context(), db=runtime_db,
        run_id="run", work_item_id="work", source_turn_id="turn",
    )
    assert result.result.error.code == "runtime_tool_binding_invalid"


@pytest.mark.parametrize("name,arguments,data", [
    ("update_plan", {"items": [{"text": "inspect", "status": "pending"}]}, {"working_plan": {}}),
    ("ask_user", {"question": "Choose?"}, {"clarification_id": "question", "status": "open"}),
])
async def test_plan_readonly_exception_survives_batch_and_frozen_execution_checks(runtime_db, monkeypatch, name, arguments, data):
    await bind(runtime_db, name)
    resolved = await resolve(runtime_db, permission="plan_readonly", mode="plan")
    async def coordinate(*args, **kwargs):
        return ToolResult(status="succeeded", summary="Plan coordination", data=data), False, None
    monkeypatch.setattr("app.runtime.action_dispatcher.dispatch_coordination_tool", coordinate)
    results = await ActionDispatcher(list(resolved.tool_definitions)).dispatch_batch(
        [ToolCallItem(call_id="call", name=name, arguments=arguments, index=0)],
        capability_snapshot=resolved.capability, runtime_context=context("plan_readonly"),
        db=runtime_db, run_id="run", work_item_id="work", source_turn_id="turn",
    )
    assert results[0].result.success
    assert results[0].invocation_id


async def test_invoke_agent_checks_agent_budget_before_creating_tool_invocation(runtime_db, monkeypatch):
    from app.services.run_reliability_service import BudgetExceeded
    await bind(runtime_db, "invoke_agent")
    resolved = await resolve(runtime_db)
    checked = []
    async def budget(db, run_id, *, action_kind):
        checked.append(action_kind)
        if action_kind == "agent":
            raise BudgetExceeded("agent_invocations", limit=1, used=1)
    monkeypatch.setattr("app.runtime.action_dispatcher.assert_action_budget", budget)
    result = await dispatch(runtime_db, resolved, "invoke_agent", arguments={"agent_id": "peer", "task": "Review"})
    assert result.result.error.code == "budget_exhausted"
    assert checked == ["tool", "agent"]
    assert await runtime_db.scalar(select(func.count()).select_from(ToolInvocation)) == 0
