"""Context profile validation and immutable registry publication contracts."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.registries_v1 import patch_policy, policy_view
from app.database import Base
from app.deps import CurrentAccount
from app.models.common import uuid7
from app.models.registry import PolicyDefinition, PolicyVersion
from app.schemas.registry_v1 import PolicyPatch
from app.services.context_policy_service import (
    CONTEXT_POLICY_DEFAULTS, LEGACY_CONTEXT_POLICY_DEFAULTS, ContextPolicyValidationError,
    context_policy_parameter_schema, resolve_context_parameters,
)
from app.services.policy_version_service import publish_policy_version
from app.services.registry_reconcile import seed_core_registries
from app.services.runtime_policy_service import resolve_frozen_runtime_policies


pytestmark = pytest.mark.unit


@pytest.fixture
async def registry_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


def test_frozen_missing_fields_keep_legacy_defaults_and_new_publications_use_current_defaults():
    legacy = {"token_budget": 6000, "max_artifacts": 0}
    resolved = resolve_context_parameters("context.standard", legacy, frozen=True)
    assert resolved == {
        "token_budget": 6000, "max_artifacts": 0,
        "input_target_ratio": 0.8, "web_result_budget_ratio": 0.2,
        "recent_history_messages": 6, "recent_tool_rounds": 2,
        "consumed_tool_result_tokens": 1024,
    }
    assert legacy == {"token_budget": 6000, "max_artifacts": 0}
    current = resolve_context_parameters("context.standard", legacy)
    assert current["recent_history_messages"] == 8
    assert current["recent_tool_rounds"] == 3
    assert resolve_context_parameters("context.standard", current, frozen=True) == current
    old_evidence = resolve_context_parameters("context.evidence", {}, frozen=True)
    assert old_evidence["recent_history_messages"] == 10
    assert old_evidence["recent_tool_rounds"] == 3
    new_evidence = resolve_context_parameters("context.evidence")
    assert new_evidence["recent_history_messages"] == 4
    assert new_evidence["recent_tool_rounds"] == 2
    assert resolve_context_parameters("context.evidence", new_evidence, frozen=True) == new_evidence
    assert resolve_context_parameters("context.concise")["input_target_ratio"] == 0.5
    assert resolve_context_parameters("context.evidence")["max_artifacts"] == 40
    schema = context_policy_parameter_schema("context.evidence")
    assert schema["properties"]["token_budget"]["maximum"] == 32_000
    assert schema["properties"]["max_artifacts"]["maximum"] == 100
    assert schema["properties"]["web_result_budget_ratio"]["default"] == 0.35
    assert schema["properties"]["recent_history_messages"]["default"] == 4
    assert not schema.get("required")


@pytest.mark.parametrize("parameters", [
    {"token_budget": 32_001}, {"token_budget": 0}, {"token_budget": True},
    {"max_artifacts": 101}, {"max_artifacts": -1}, {"max_artifacts": "20"},
    {"input_target_ratio": 0.09}, {"input_target_ratio": 0.91},
    {"web_result_budget_ratio": 0.04}, {"web_result_budget_ratio": 0.51},
    {"input_target_ratio": float("nan")}, {"input_target_ratio": float("inf")},
    {"input_target_ratio": None}, {"web_result_budget_ratio": False},
    {"recent_history_messages": -1}, {"recent_history_messages": 41},
    {"recent_tool_rounds": 0}, {"recent_tool_rounds": True},
    {"consumed_tool_result_tokens": 127}, {"consumed_tool_result_tokens": 8193},
    {"hard_input_limit": 1_000_000}, [],
])
def test_profiles_reject_invalid_parameters_and_hard_limit_overrides(parameters):
    with pytest.raises(ContextPolicyValidationError):
        resolve_context_parameters("context.standard", parameters)


def test_unknown_context_profile_is_not_a_custom_code_hook():
    with pytest.raises(ContextPolicyValidationError, match="unsupported"):
        resolve_context_parameters("context.custom", {})


@pytest.mark.asyncio
async def test_seed_preserves_legacy_version_and_adds_profiles_idempotently(registry_db):
    db = registry_db
    legacy = PolicyDefinition(
        id=uuid7(), policy_key="context.standard", policy_type="context",
        name="Customized standard", description="Existing deployment policy",
        implementation_ref="app.runtime.policies:context.standard",
        implementation_version="1", default_parameters={"token_budget": 6000},
        parameter_version=7, enabled=True, status="active", next_version_no=1,
    )
    db.add(legacy)
    await db.flush()
    frozen = await publish_policy_version(db, legacy)
    frozen_id, frozen_hash = frozen.id, frozen.content_hash
    await seed_core_registries(db)
    await db.flush()
    first_count = await db.scalar(select(func.count()).select_from(PolicyVersion))
    await seed_core_registries(db)
    await db.flush()
    assert await db.scalar(select(func.count()).select_from(PolicyVersion)) == first_count
    profiles = {row.policy_key: row for row in await db.scalars(
        select(PolicyDefinition).where(PolicyDefinition.policy_type == "context")
    )}
    assert set(profiles) == set(CONTEXT_POLICY_DEFAULTS)
    assert legacy.current_version_id == frozen_id
    assert legacy.parameter_version == 7
    assert legacy.default_parameters == {"token_budget": 6000}
    assert frozen.content_hash == frozen_hash
    assert frozen.parameters == {"token_budget": 6000}
    for key in ("context.concise", "context.evidence"):
        version = await db.get(PolicyVersion, profiles[key].current_version_id)
        assert version.parameters == CONTEXT_POLICY_DEFAULTS[key]
        assert policy_view(profiles[key])["parameter_schema"]["additionalProperties"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("key,parameters", [
    ("context.standard", {"token_budget": 8000}),
    ("context.standard", LEGACY_CONTEXT_POLICY_DEFAULTS["context.standard"]),
    ("context.evidence", LEGACY_CONTEXT_POLICY_DEFAULTS["context.evidence"]),
])
async def test_seed_advances_untouched_defaults_once_without_changing_frozen_behavior(
    registry_db, key, parameters,
):
    db = registry_db
    policy = PolicyDefinition(
        id=uuid7(), policy_key=key, policy_type="context", name=key, description=key,
        implementation_ref=f"app.runtime.policies:{key}", implementation_version="1",
        default_parameters=dict(parameters), parameter_version=1,
        enabled=True, status="active", next_version_no=1,
    )
    db.add(policy)
    await db.flush()
    frozen = await publish_policy_version(db, policy)
    old_id, old_hash = frozen.id, frozen.content_hash
    old_effective = resolve_context_parameters(key, frozen.parameters, frozen=True)
    await seed_core_registries(db)
    current = await db.get(PolicyVersion, policy.current_version_id)
    assert current.id != old_id
    assert current.parameters == CONTEXT_POLICY_DEFAULTS[key]
    assert policy.default_parameters == current.parameters
    assert policy.parameter_version == 2
    assert frozen.parameters == parameters and frozen.content_hash == old_hash
    assert resolve_context_parameters(key, frozen.parameters, frozen=True) == old_effective
    assert resolve_context_parameters(key, current.parameters, frozen=True) == CONTEXT_POLICY_DEFAULTS[key]
    await seed_core_registries(db)
    assert policy.current_version_id == current.id and policy.parameter_version == 2
    assert await db.scalar(select(func.count()).select_from(PolicyVersion).where(
        PolicyVersion.policy_definition_id == policy.id,
    )) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"parameter_version": 2},  # Admin changed and even restored the old numbers.
    {"default_parameters": {"token_budget": 6000}},
    {"implementation_ref": "custom:context.standard"},
    {"enabled": False}, {"status": "inactive"},
])
async def test_seed_leaves_admin_customizations_and_disabled_policies_unchanged(registry_db, change):
    db = registry_db
    values = {
        "id": uuid7(), "policy_key": "context.standard", "policy_type": "context",
        "name": "Existing policy", "description": "Existing policy",
        "implementation_ref": "app.runtime.policies:context.standard",
        "implementation_version": "1", "default_parameters": {"token_budget": 8000},
        "parameter_version": 1, "enabled": True, "status": "active", "next_version_no": 1,
    }
    policy = PolicyDefinition(**{**values, **change})
    db.add(policy)
    await db.flush()
    frozen = await publish_policy_version(db, policy)
    old_id, parameters, revision = frozen.id, dict(policy.default_parameters), policy.parameter_version
    await seed_core_registries(db)
    assert policy.current_version_id == old_id
    assert policy.default_parameters == parameters and policy.parameter_version == revision
    assert frozen.parameters == parameters


@pytest.mark.asyncio
async def test_policy_patch_publishes_once_and_preserves_frozen_agent_bindings(registry_db):
    db = registry_db
    await seed_core_registries(db)
    await db.commit()
    rows = {row.policy_key: row for row in await db.scalars(select(PolicyDefinition))}
    policy = rows["context.standard"]
    previous_id = policy.current_version_id
    previous = await db.get(PolicyVersion, previous_id)
    previous_parameters = dict(previous.parameters)
    current = CurrentAccount(
        account=SimpleNamespace(id="policy-admin", user_id="policy-admin", tenant_id="default"),
        roles=frozenset({"admin"}),
    )
    bindings = {}
    for policy_type, key in (
        ("context", "context.standard"), ("history", "history.recent"),
        ("memory_write", "memory_write.none"), ("memory_recall", "memory_recall.none"),
    ):
        bindings[f"{policy_type}_policy_id"] = rows[key].id
        bindings[f"{policy_type}_policy_version_id"] = rows[key].current_version_id
    old_agent = SimpleNamespace(**bindings)
    arguments = {
        "policy_id": policy.id,
        "body": PolicyPatch(default_parameters={"input_target_ratio": 0.6, "max_artifacts": 3}),
        "if_match": "1", "idempotency_key": "publish-context-v2", "db": db, "current": current,
    }
    response = await patch_policy(**arguments)
    assert response["current_version_id"] != previous_id
    published = await db.get(PolicyVersion, response["current_version_id"])
    assert published.version_no == 2
    assert published.parameters["input_target_ratio"] == 0.6
    assert published.parameters["token_budget"] == 8000
    assert response["default_parameters"] == published.parameters
    assert previous.parameters == previous_parameters
    resolved = await resolve_frozen_runtime_policies(db, old_agent)
    assert resolved["context"].version.id == previous_id
    assert resolved["context"].version.parameters == previous_parameters
    assert await patch_policy(**arguments) == response
    assert await db.scalar(select(func.count()).select_from(PolicyVersion).where(
        PolicyVersion.policy_definition_id == policy.id,
    )) == 2

    reverted = await patch_policy(
        policy_id=policy.id, body=PolicyPatch(default_parameters=previous_parameters),
        if_match="2", idempotency_key="restore-context-v1", db=db, current=current,
    )
    assert reverted["current_version_id"] == previous_id
    assert policy.next_version_no == 3
    assert published.parameters["input_target_ratio"] == 0.6


@pytest.mark.asyncio
async def test_invalid_policy_patch_does_not_publish(registry_db):
    db = registry_db
    await seed_core_registries(db)
    await db.commit()
    policy = await db.scalar(select(PolicyDefinition).where(
        PolicyDefinition.policy_key == "context.standard",
    ))
    previous_id = policy.current_version_id
    current = CurrentAccount(
        account=SimpleNamespace(id="policy-admin", user_id="policy-admin", tenant_id="default"),
        roles=frozenset({"admin"}),
    )
    with pytest.raises(HTTPException) as rejected:
        await patch_policy(
            policy_id=policy.id, body=PolicyPatch(default_parameters={"token_budget": 100_000}),
            if_match="1", idempotency_key="invalid-context", db=db, current=current,
        )
    assert rejected.value.status_code == 422
    assert policy.current_version_id == previous_id
    assert policy.parameter_version == 1
    assert await db.scalar(select(func.count()).select_from(PolicyVersion).where(
        PolicyVersion.policy_definition_id == policy.id,
    )) == 1
