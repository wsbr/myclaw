"""Schema presentation, progressive disclosure and live Tool governance."""

from copy import deepcopy
import json

import pytest

from app.core.tool_registry.base import ToolResult
from app.core.tool_registry.builtin.schedule_task import ScheduleTaskTool
from app.models.agent_v1 import AgentVersionTool
from app.models.registry import ToolGovernance
from app.models.runtime_v1 import DurableRunner, Run
from app.runtime.action_dispatcher import ActionDispatcher
from app.runtime.agent_loop_coordinator import AgentLoopCoordinator, LoopIdentity
from app.runtime.context_assembler import ContextAssembler, ContextPackage
from app.runtime.protocol import AssistantMessageItem, ContentPart, NormalizedTurnResult, StopReason, ToolCallItem
from app.runtime.snapshot_resolver import SnapshotResolver
from app.runtime.tool_presentation import project_tool_definition, resolve_tool_presentation
from app.services.runtime_resume_service import tool_continuation
from test.unit.backend.test_runtime_tool_authorization import bind, context, dispatch, resolve, runtime_db

pytestmark = pytest.mark.unit
SCHEDULE_REF = "app.core.tool_registry.builtin.schedule_task:ScheduleTaskTool"


def test_scheduled_projection_narrows_the_trusted_contract_without_mutating_it():
    original = {
        "name": "schedule_task", "description": ScheduleTaskTool.description,
        "implementation_ref": SCHEDULE_REF, "input_schema": deepcopy(ScheduleTaskTool.input_schema),
        "side_effect": "idempotent", "tool_version_id": "frozen", "tool_version_content_hash": "hash",
    }
    projected = project_tool_definition(original, authentication_source="schedule")
    assert projected["input_schema"]["properties"]["operation"]["enum"] == ["list"]
    assert set(projected["input_schema"]["properties"]) == {"operation", "status", "limit"}
    assert projected["side_effect"] == "none"
    assert projected["tool_version_id"] == "frozen" and projected["tool_version_content_hash"] == "hash"
    assert "operation=list" in projected["description"]
    assert original["input_schema"]["properties"]["operation"]["enum"] != ["list"]
    assert project_tool_definition(original, authentication_source="web_session") == original
    forged = {**original, "implementation_ref": "third_party:Schedule"}
    assert project_tool_definition(forged, authentication_source="schedule") == forged


@pytest.mark.parametrize("permission", ["direct", "readonly"])
async def test_schedule_initial_search_resume_and_execution_share_list_contract(runtime_db, permission):
    db = runtime_db
    await bind(db, "search_tools")
    _, version = await bind(db, "schedule_task")
    version.implementation_ref = SCHEDULE_REF
    version.input_schema = deepcopy(ScheduleTaskTool.input_schema)
    version.side_effect = "idempotent"
    version.execution_mode = "durable_async"
    runner = await db.get(DurableRunner, "runner")
    runner.principal_snapshot = {"tenant_id": "tenant", "account_id": "account", "mode": "chat", "authentication_source": "schedule"}
    (await db.get(Run, "run")).permission_mode = permission
    resolved = await SnapshotResolver().resolve(
        db, runner=runner, execution_principal=runner.principal_snapshot, permission_mode=permission,
    )
    runtime_context = context(permission).model_copy(update={"authentication_source": "schedule"})
    dispatcher = ActionDispatcher(list(resolved.tool_definitions))
    async def invoke(name, args, call_id):
        return await dispatcher.dispatch(
            ToolCallItem(call_id, name, args, 0), capability_snapshot=resolved.capability,
            runtime_context=runtime_context, db=db, run_id="run", work_item_id="work", source_turn_id="turn",
        )
    initial = next(item for item in resolved.tool_definitions if item["name"] == "schedule_task")
    searched = await invoke("search_tools", {"query": "schedule"}, "search")
    assert searched.result.success
    loaded = next(item for item in searched.result.data["loaded_tool_definitions"] if item["name"] == "schedule_task")
    resumed = await resolve_tool_presentation(
        db, capability_snapshot=resolved.capability, authentication_source="schedule",
        tool_definitions=list(resolved.capability.authorized_tool_versions),
    )
    restored = next(item for item in resumed.tool_definitions if item["name"] == "schedule_task")
    for definition in (initial, loaded, restored):
        assert definition["input_schema"]["properties"]["operation"]["enum"] == ["list"]
        assert "operation=list" in definition["description"]
    queued = await invoke("schedule_task", {"operation": "list", "limit": 3}, "list")
    assert queued.result.success and queued.durable_wait
    for operation in ("create", "update", "pause", "resume", "delete", "run_now"):
        denied = await invoke("schedule_task", {"operation": operation}, operation)
        assert denied.result.error.code == "schedule_recursive_mutation_denied"
        assert not denied.durable_wait


@pytest.mark.parametrize("name", ["knowledge_search", "update_plan"])
async def test_live_disable_removes_schema_marks_search_blocked_and_denies_execution(runtime_db, name):
    db = runtime_db
    await bind(db, "search_tools")
    tool, _ = await bind(db, name, eager=False)
    resolved = await resolve(db)
    target = next(item for item in resolved.capability.authorized_tool_versions if item["name"] == name)
    before = await dispatch(db, resolved, "search_tools", arguments={"query": name}, call_id="search")
    assert next(item for item in before.result.data["tools"] if item["name"] == name)["execution_available"]
    (await db.get(ToolGovernance, tool.id)).enabled = False
    await db.flush()
    presented = await resolve_tool_presentation(db, capability_snapshot=resolved.capability, authentication_source="web_session")
    assert name not in {item["name"] for item in presented.tool_definitions}
    searched = await dispatch(db, resolved, "search_tools", arguments={"query": name}, call_id="search")
    assert searched.invocation_id == before.invocation_id
    item = next(item for item in searched.result.data["tools"] if item["name"] == name)
    assert not item["execution_available"] and item["blocked_reason"] == "platform_tool_disabled"
    resumed = await tool_continuation(db, work_item_id="work", source_turn_id="turn")
    result = json.loads(resumed[-1]["content"])
    assert result["data"]["loaded_tool_definitions"] == []
    assert not next(item for item in result["data"]["tools"] if item["name"] == name)["execution_available"]
    assert not next(item for item in result["content_blocks"][0]["value"] if item["name"] == name)["execution_available"]
    executed = []
    async def executor(*args):
        executed.append(args[0])
        return ToolResult(status="succeeded", summary="should not execute")
    result = await dispatch(db, resolved, name, definition=target, executor=executor)
    assert result.result.error.code == "platform_tool_disabled"
    assert not executed


@pytest.mark.parametrize("change", ["hash", "binding", "disabled", "confirmation", "risk_rule"])
async def test_inline_tool_uses_live_policy_and_frozen_identity(runtime_db, change):
    db = runtime_db
    tool, _ = await bind(db, "knowledge_search", eager=True)
    resolved = await resolve(db)
    target = dict(resolved.tool_definitions[0])
    policy = await db.get(ToolGovernance, tool.id)
    if change == "hash":
        target["tool_version_content_hash"] = "forged"
    elif change == "binding":
        await db.delete(await db.get(AgentVersionTool, ("assembly", tool.id)))
    elif change == "disabled":
        policy.enabled = False
    elif change == "confirmation":
        policy.confirmation_policy = "per_call"
    else:
        policy.risk_rules = [{"when": "always", "confirmation": "per_call"}]
    await db.flush()
    executed = []
    async def executor(*args):
        executed.append(args[0])
        return ToolResult(status="succeeded", summary="should not execute")
    result = await dispatch(db, resolved, "knowledge_search", definition=target, executor=executor)
    if change in {"confirmation", "risk_rule"}:
        assert result.durable_wait and result.wait_reason == "approval"
    else:
        assert result.result.error.code == ("platform_tool_disabled" if change == "disabled" else "tool_binding_invalid")
    assert not executed


async def test_loaded_names_cannot_replace_frozen_schemas_or_cross_version_boundaries(runtime_db):
    await bind(runtime_db, "knowledge_search", eager=False)
    resolved = await resolve(runtime_db)
    target = dict(resolved.capability.authorized_tool_versions[0])
    poisoned = {**target, "input_schema": {"description": "poison"}, "description": "poison"}
    presentation = await resolve_tool_presentation(
        runtime_db, capability_snapshot=resolved.capability, authentication_source="web_session", tool_definitions=[poisoned],
    )
    assert presentation.tool_definitions[0]["input_schema"] == target["input_schema"]
    assert presentation.tool_definitions[0]["description"] == target["description"]
    foreign = {**poisoned, "tool_version_id": "another-agents-version"}
    presentation = await resolve_tool_presentation(
        runtime_db, capability_snapshot=resolved.capability, authentication_source="web_session", tool_definitions=[foreign],
    )
    assert presentation.tool_definitions == ()


async def test_disabling_search_keeps_other_effective_bindings_reachable(runtime_db):
    db = runtime_db
    search, _ = await bind(db, "search_tools")
    await bind(db, "knowledge_search", eager=False)
    disabled, _ = await bind(db, "other_probe", eager=False)
    resolved = await resolve(db)
    assert [item["name"] for item in resolved.tool_definitions] == ["search_tools"]
    (await db.get(ToolGovernance, search.id)).enabled = False
    (await db.get(ToolGovernance, disabled.id)).enabled = False
    await db.flush()
    presentation = await resolve_tool_presentation(
        db, capability_snapshot=resolved.capability, authentication_source="web_session",
        tool_definitions=list(resolved.tool_definitions),
    )
    assert [item["name"] for item in presentation.tool_definitions] == ["knowledge_search"]
    assert presentation.effective_tool_keys == {"knowledge_search"}


async def test_coordinator_never_reinjects_full_schedule_schema_from_search(runtime_db):
    db = runtime_db
    await bind(db, "search_tools")
    _, version = await bind(db, "schedule_task")
    version.implementation_ref = SCHEDULE_REF
    version.input_schema = deepcopy(ScheduleTaskTool.input_schema)
    resolved = await resolve(db)
    search_result = await dispatch(db, resolved, "search_tools", arguments={"query": "schedule"})
    # Search result was captured from an unrestricted context. Presentation must
    # reproject the canonical contract for this scheduled model request.
    class Adapter:
        provider = object()
        max_zero_payload_retries = 0
        def __init__(self): self.requests = []
        async def invoke(self, request, **kwargs):
            self.requests.append(request)
            if len(self.requests) == 1:
                return NormalizedTurnResult((ToolCallItem("call", "search_tools", {}, 0),), StopReason.TOOL_USE)
            return NormalizedTurnResult((AssistantMessageItem((ContentPart("text", "done"),)),), StopReason.END_TURN)
    class Dispatcher:
        async def dispatch_batch(self, *args, **kwargs): return [search_result]
        def add_definitions(self, definitions): pass
    adapter = Adapter()
    await AgentLoopCoordinator(adapter, Dispatcher()).run(
        context=ContextPackage(({"role": "user", "content": "test"},), (), 1),
        tool_definitions=[item for item in resolved.tool_definitions if item["name"] == "search_tools"],
        capability_snapshot=resolved.capability,
        runtime_context=context().model_copy(update={"authentication_source": "schedule"}), model_parameters={},
    )
    assert [item["name"] for item in adapter.requests[0].tools] == ["search_tools"]
    target = next(item for item in adapter.requests[1].tools if item["name"] == "schedule_task")
    assert target["input_schema"]["properties"]["operation"]["enum"] == ["list"]
    assert json.loads(adapter.requests[1].messages[-1]["content"])["data"]["loaded_tool_definitions"] == []


async def test_each_provider_turn_refreshes_disabled_schema_and_its_guidance(runtime_db):
    db = runtime_db
    await bind(db, "search_tools")
    tool, _ = await bind(db, "knowledge_search", eager=True)
    resolved = await resolve(db)
    class Adapter:
        provider = object()
        max_zero_payload_retries = 0
        def __init__(self): self.requests = []
        async def invoke(self, request, **kwargs):
            self.requests.append(request)
            if len(self.requests) == 1:
                (await db.get(ToolGovernance, tool.id)).enabled = False
                await db.commit()
                return NormalizedTurnResult((ToolCallItem("search", "search_tools", {"query": "knowledge"}, 0),), StopReason.TOOL_USE)
            return NormalizedTurnResult((AssistantMessageItem((ContentPart("text", "done"),)),), StopReason.END_TURN)
    adapter = Adapter()
    package = ContextAssembler().assemble(
        platform_rules="Platform rules", agent_instructions="", current_input="Review evidence",
        permission_summary={"mode": "direct", "presented_tool_keys": ["search_tools", "knowledge_search"]},
        tool_guidance_specs=({"requires": ["knowledge_search"], "content": "KNOWLEDGE_GUIDANCE"},),
        tool_definitions=resolved.tool_definitions,
    )
    # No external token counter or provider is needed for this boundary test.
    from dataclasses import replace
    package = replace(package, hard_input_limit=None)
    await AgentLoopCoordinator(adapter, ActionDispatcher(list(resolved.tool_definitions))).run(
        context=package, tool_definitions=list(resolved.tool_definitions), capability_snapshot=resolved.capability,
        runtime_context=context(), model_parameters={}, db=db,
        identity=LoopIdentity("runner", "work", "run", "assembly", "model-version", "openai"),
    )
    assert {item["name"] for item in adapter.requests[0].tools} == {"search_tools", "knowledge_search"}
    assert [item["name"] for item in adapter.requests[1].tools] == ["search_tools"]
    assert any(item.get("content") == "KNOWLEDGE_GUIDANCE" for item in adapter.requests[0].messages)
    assert not any(item.get("content") == "KNOWLEDGE_GUIDANCE" for item in adapter.requests[1].messages)
    boundary = json.loads(adapter.requests[1].messages[0]["content"].rsplit("Execution boundary: ", 1)[1])
    assert boundary["presented_tool_keys"] == ["search_tools"]
