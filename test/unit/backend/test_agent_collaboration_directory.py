"""Network discovery and bound-target projection use independent authorization paths."""

from copy import deepcopy

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.tool_registry.runtime_tools import RUNTIME_TOOLS
from app.database import Base
from app.models.agent_v1 import (
    AgentAssemblyVersion, AgentBlueprint, AgentCard, AgentVersionBinding, AgentVersionTool,
)
from app.models.common import utcnow
from app.models.registry import ModelVersion, ToolDefinition, ToolGovernance, ToolVersion
from app.models.runtime_v1 import DurableRunner
from app.runtime.snapshot_resolver import SnapshotResolver
from app.services.agent_directory_service import discover_agent_cards, resolve_bound_agent_cards

pytestmark = pytest.mark.unit


@pytest.fixture
async def directory_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async def policies(*args, **kwargs):
        return {}
    monkeypatch.setattr("app.runtime.snapshot_resolver.resolve_frozen_runtime_policies", policies)
    async with factory() as db:
        yield db
    await engine.dispose()


async def agent(db, name, *, network=False, visibility="enterprise", available=True, governance=None):
    blueprint = AgentBlueprint(
        id=name, tenant_id="tenant", owner_account_id="owner", visibility=visibility,
        status="published", current_published_version_id=f"{name}-v1",
    )
    version = AgentAssemblyVersion(
        id=f"{name}-v1", agent_id=name, version_no=1, publication_status="published",
        name=name, created_by_account_id="owner", immutable_at=utcnow(),
        group_enabled=network, model_version_id="model-version",
    )
    card = AgentCard(
        id=f"{name}-card", agent_id=name, agent_version_id=version.id,
        visibility=visibility, name=name, group_enabled=network,
        availability="available" if available else "unavailable",
        collaboration_summary={"governance": governance or {}},
        content_hash=name.ljust(64, "x"), published_at=utcnow(),
    )
    db.add_all([blueprint, version, card])
    await db.flush()
    return blueprint, version, card


async def discover(db, **overrides):
    kwargs = dict(
        tenant_id="tenant", account_id="requester", actor_agent_id="caller",
        actor_agent_version_id="caller-v1",
    )
    return await discover_agent_cards(db, **{**kwargs, **overrides})


async def test_disabled_caller_cannot_discover_even_with_explicit_true_filter(directory_db):
    db = directory_db
    await agent(db, "caller")
    await agent(db, "peer", network=True)
    assert await discover(db) == []
    assert await discover(db, group_enabled=True) == []


async def test_network_discovery_only_exposes_eligible_network_peers(directory_db):
    db = directory_db
    await agent(db, "caller", network=True)
    await agent(db, "peer", network=True)
    await agent(db, "disabled")
    await agent(db, "private", network=True, visibility="private")
    await agent(db, "unavailable", network=True, available=False)
    for key in ("discoverable", "callable", "group_allowed"):
        await agent(db, key, network=True, governance={key: False})
    assert [card.agent_id for card in await discover(db)] == ["peer"]
    assert await discover(db, group_enabled=False) == []
    assert await discover(db, actor_agent_version_id="peer-v1") == []
    assert await discover(db, tenant_id="another-tenant") == []


async def test_current_enabled_version_does_not_enable_old_frozen_caller(directory_db):
    db = directory_db
    blueprint, version, _ = await agent(db, "caller")
    await agent(db, "peer", network=True)
    newer = AgentAssemblyVersion(
        id="caller-v2", agent_id="caller", version_no=2, publication_status="published",
        name="caller", created_by_account_id="owner", immutable_at=utcnow(), group_enabled=True,
    )
    db.add(newer)
    blueprint.current_published_version_id = newer.id
    await db.flush()
    assert await discover(db, actor_agent_version_id=version.id) == []


async def test_bound_projection_is_per_hop_and_still_checks_target_access(directory_db):
    db = directory_db
    for name in ("caller", "child", "grandchild"):
        await agent(db, name)
    await agent(db, "private", visibility="private")
    await agent(db, "denied", governance={"callable": False})
    db.add_all([
        AgentVersionBinding(agent_version_id="caller-v1", bound_agent_id="child", sort_order=0),
        AgentVersionBinding(agent_version_id="caller-v1", bound_agent_id="private", sort_order=1),
        AgentVersionBinding(agent_version_id="caller-v1", bound_agent_id="denied", sort_order=2),
        AgentVersionBinding(agent_version_id="child-v1", bound_agent_id="grandchild", sort_order=0),
    ])
    await db.flush()
    for caller, expected in (("caller", "child"), ("child", "grandchild")):
        cards = await resolve_bound_agent_cards(
            db, tenant_id="tenant", account_id="requester", actor_agent_id=caller,
            actor_agent_version_id=f"{caller}-v1",
        )
        assert [card.agent_id for card in cards] == [expected]


@pytest.mark.parametrize("network", [False, True])
@pytest.mark.parametrize("invocation_enabled", [False, True])
async def test_snapshot_separates_bound_targets_from_network_discovery(directory_db, network, invocation_enabled):
    db = directory_db
    _, version, _ = await agent(db, "caller", network=network)
    await agent(db, "bound")
    await agent(db, "peer", network=True)
    db.add_all([
        ModelVersion(id="model-version", model_id="model", version_no=1, provider="openai",
                     model_name="test", content_hash="m" * 64, published_at=utcnow()),
        AgentVersionBinding(agent_version_id=version.id, bound_agent_id="bound", sort_order=0),
    ])
    for index, name in enumerate(("discover_agents", "select_group_members", "invoke_agent")):
        contract = deepcopy(RUNTIME_TOOLS[name])
        tool = ToolDefinition(id=f"tool-{name}", tool_key=name, namespace="runtime",
                              origin="builtin", display_name=name, current_version_id=f"tool-version-{name}")
        tool_version = ToolVersion(
            id=f"tool-version-{name}", definition_id=tool.id, version_no=1,
            model_description=contract["description"], input_schema=contract["input_schema"],
            output_schema=contract["output_schema"], annotations=contract["annotations"],
            implementation_ref=contract["implementation_ref"], implementation_version="test",
            execution_mode=contract["execution_mode"], side_effect=contract["side_effect"],
            content_hash=contract["registration_hash"],
        )
        db.add_all([tool, tool_version, ToolGovernance(
                        tool_definition_id=tool.id, enabled=name != "invoke_agent" or invocation_enabled,
                    ),
                    AgentVersionTool(agent_version_id=version.id, platform_tool_id=tool.id,
                                     tool_version_id=tool_version.id,
                                     tool_version_content_hash=tool_version.content_hash, sort_order=index)])
    runner = DurableRunner(
        id="runner", tenant_id="tenant", work_item_id="work", run_id="run", session_id="session",
        agent_id="caller", agent_version_id=version.id, execution_epoch=1,
    )
    db.add(runner)
    await db.flush()
    snapshot = await SnapshotResolver().resolve(
        db, runner=runner, execution_principal={"tenant_id": "tenant", "account_id": "requester", "mode": "chat"},
        permission_mode="direct",
    )
    cap = snapshot.capability
    assert cap.discoverable_agents_scope["agent_ids"] == (["peer"] if network else [])
    expected_callable = (["bound", "peer"] if network else ["bound"]) if invocation_enabled else []
    assert cap.callable_agents_scope["agent_ids"] == expected_callable
    assert cap.callable_agents_scope["bound_agents"] == (
        [{"agent_id": "bound", "name": "bound", "description": ""}] if invocation_enabled else []
    )
    assert ("invoke_agent" in cap.effective_tool_keys) is invocation_enabled
    for name in ("discover_agents", "select_group_members"):
        assert (name in cap.effective_tool_keys) is network
