"""Runtime contracts use the same catalog, governance and binding surfaces."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.agent_options import tools as agent_tool_options
from app.api.registries_v1 import patch_platform_tool
from app.core.tool_registry.builtin.search_group_history import SearchGroupHistoryTool
from app.core.tool_registry.runtime_tools import (
    RUNTIME_COORDINATION_DEFINITIONS, RUNTIME_PLAN_READONLY_TOOLS,
    RUNTIME_TOOLS, is_runtime_tool_implementation,
)
from app.core.tool_registry.schema import assert_valid_schema, builtin_result_schema
from app.database import Base
from app.deps import CurrentAccount
from app.models.agent_v1 import AgentVersionTool
from app.models.registry import ToolDefinition, ToolGovernance, ToolVersion
from app.schemas.registry_v1 import PlatformToolPatch
from app.services.registry_reconcile import CodeToolSpec, builtin_tool_specs, reconcile_platform_tools


pytestmark = pytest.mark.unit


@pytest.fixture
async def catalog_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


def _admin():
    return CurrentAccount(
        account=SimpleNamespace(id="catalog-admin", user_id="catalog-admin", tenant_id="default"),
        roles=frozenset({"admin"}),
    )


def _legacy_group_history_spec():
    return CodeToolSpec(
        tool_key="search_group_history", display_name="Search Group History",
        display_name_zh="搜索群组历史", description=SearchGroupHistoryTool.description,
        input_schema=SearchGroupHistoryTool.input_schema,
        output_schema=builtin_result_schema("search_group_history"),
        implementation_ref="app.core.tool_registry.builtin.search_group_history:SearchGroupHistoryTool",
    )


def test_all_runtime_contracts_have_one_localized_governed_registry_entry():
    expected = {
        "search_tools", "update_plan", "find_agent_user", "ask_user", "discover_agents",
        "select_group_members", "invoke_agent", "search_group_history", "send_group_message",
        "invite_agent", "update_shared_task",
    }
    specs = builtin_tool_specs()
    runtime_specs = {spec.tool_key: spec for spec in specs if spec.namespace == "runtime"}
    assert set(runtime_specs) == expected == set(RUNTIME_TOOLS)
    assert len({spec.tool_key for spec in specs}) == len(specs)
    assert len(RUNTIME_COORDINATION_DEFINITIONS) == 11
    for name, spec in runtime_specs.items():
        assert spec.origin == "builtin" and spec.execution_mode == "inline"
        assert spec.display_name_zh and spec.initial_confirmation_policy == "never"
        assert spec.annotations["runtime_owned"] and spec.annotations["eager"]
        assert is_runtime_tool_implementation(name, spec.implementation_ref)
        assert_valid_schema(spec.input_schema, label=f"{name} input")
        assert_valid_schema(spec.output_schema, label=f"{name} output")
    assert runtime_specs["select_group_members"].annotations["runtime_modes"] == ["chat"]
    for name in {"search_group_history", "send_group_message", "invite_agent", "update_shared_task"}:
        assert runtime_specs[name].annotations["runtime_modes"] == ["group"]
    assert RUNTIME_PLAN_READONLY_TOOLS == {"update_plan", "ask_user"}
    assert runtime_specs["update_plan"].side_effect == "non_idempotent"
    assert runtime_specs["ask_user"].side_effect == "idempotent"
    assert runtime_specs["invoke_agent"].side_effect == "idempotent"
    assert not is_runtime_tool_implementation("ask_user", "app.runtime.coordination_tools:invoke_agent")
    assert not is_runtime_tool_implementation("ask_user", "mcp://connection/ask_user")
    assert not is_runtime_tool_implementation("unknown", None)


@pytest.mark.asyncio
async def test_runtime_reconcile_preserves_definition_governance_and_frozen_binding(catalog_db):
    db = catalog_db
    legacy = (await reconcile_platform_tools(db, [_legacy_group_history_spec()], job_run_id="old"))[0]
    definition_id, version_id = legacy.id, legacy.current_version_id
    old_version = await db.get(ToolVersion, version_id)
    old_hash = old_version.content_hash
    binding = AgentVersionTool(
        agent_version_id="existing-published-version", platform_tool_id=definition_id,
        tool_version_id=version_id, tool_version_content_hash=old_hash, sort_order=0,
    )
    db.add(binding)
    governance = await db.get(ToolGovernance, definition_id)
    governance.enabled = False
    governance.confirmation_policy = "per_call"
    await db.flush()
    specs = builtin_tool_specs()
    await reconcile_platform_tools(db, specs, job_run_id="new")
    first_count = await db.scalar(select(func.count()).select_from(ToolVersion))
    await reconcile_platform_tools(db, specs, job_run_id="replay")
    assert await db.scalar(select(func.count()).select_from(ToolVersion)) == first_count
    assert await db.scalar(select(func.count()).select_from(ToolDefinition).where(
        ToolDefinition.tool_key == "search_group_history",
    )) == 1
    assert legacy.id == definition_id and legacy.current_version_id != version_id
    assert legacy.namespace == "runtime"
    assert not governance.enabled and governance.confirmation_policy == "per_call"
    assert binding.tool_version_id == version_id and binding.tool_version_content_hash == old_hash
    assert old_version.content_hash == old_hash
    assert old_version.implementation_ref == _legacy_group_history_spec().implementation_ref
    assert await db.scalar(select(func.count()).select_from(AgentVersionTool)) == 1


@pytest.mark.asyncio
async def test_runtime_tools_are_available_as_explicit_agent_options_and_respect_disable(catalog_db):
    db = catalog_db
    await reconcile_platform_tools(db, builtin_tool_specs(), job_run_id="catalog")
    rows = await agent_tool_options(db=db, _=_admin())
    by_key = {row["key"]: row for row in rows}
    assert set(RUNTIME_TOOLS) <= set(by_key)
    assert by_key["ask_user"]["annotations"]["runtime_modes"] == ["chat", "plan", "group"]
    assert by_key["ask_user"]["display_name_zh"] == "询问用户"
    governance = await db.get(ToolGovernance, by_key["ask_user"]["id"])
    governance.enabled = False
    await db.flush()
    rows = await agent_tool_options(db=db, _=_admin())
    assert "ask_user" not in {row["key"] for row in rows}
    assert await db.scalar(select(func.count()).select_from(AgentVersionTool)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"confirmation_policy": "per_call"},
    {"confirmation_policy": "session_grant"},
    {"risk_rules": [{"when": {"argument": "question"}, "confirmation": "per_call"}]},
])
async def test_runtime_confirmation_policies_are_rejected(catalog_db, changes):
    db = catalog_db
    tools = await reconcile_platform_tools(db, builtin_tool_specs(), job_run_id="catalog")
    tool = next(row for row in tools if row.tool_key == "ask_user")
    governance = await db.get(ToolGovernance, tool.id)
    with pytest.raises(HTTPException) as caught:
        await patch_platform_tool(
            tool.id, PlatformToolPatch(**changes), if_match=str(governance.version),
            idempotency_key="reject-policy", db=db, current=_admin(),
        )
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "runtime_confirmation_policy_unsupported"
    assert governance.confirmation_policy == "never" and governance.risk_rules == []


@pytest.mark.asyncio
async def test_regular_tool_confirmation_policy_remains_configurable(catalog_db):
    db = catalog_db
    tools = await reconcile_platform_tools(db, builtin_tool_specs(), job_run_id="catalog")
    tool = next(row for row in tools if row.tool_key == "write_file")
    governance = await db.get(ToolGovernance, tool.id)
    result = await patch_platform_tool(
        tool.id, PlatformToolPatch(confirmation_policy="per_call"),
        if_match=str(governance.version), idempotency_key="set-policy",
        db=db, current=_admin(),
    )
    assert result["confirmation_policy"] == "per_call"


@pytest.mark.asyncio
async def test_reconcile_rejects_duplicate_tool_keys_instead_of_shadowing(catalog_db):
    with pytest.raises(ValueError, match="one contract per tool_key"):
        await reconcile_platform_tools(
            catalog_db, [_legacy_group_history_spec(), _legacy_group_history_spec()],
            job_run_id="duplicate",
        )
