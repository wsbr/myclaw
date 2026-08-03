"""Agent draft/publish state machine tests for the phase-one model."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.api.agents import (
    _change_agent_state,
    _get_accessible_agent,
    _get_owned_agent,
    _idempotency_record,
    _if_match_revision,
    _summary,
)
from app.deps import CurrentAccount
from app.models.agent_v1 import (
    AgentAssemblyVersion,
    AgentBlueprint,
    AgentVersionBinding,
    AgentVersionSkill,
    AgentVersionTool,
)
from app.models.common import utcnow, uuid7
from app.models.foundation import (
    IdempotencyRecord,
    PlatformAccount,
    PlatformAuditEvent,
    UserIdentity,
)
from app.models.registry import (
    PlatformTool,
    PolicyDefinition,
    RegisteredModel,
    RegisteredSkill,
    SandboxProfile,
)
from app.services.agent_assembly_service import (
    AgentDraftConflict,
    AgentPublishValidationError,
    AssemblySelection,
    create_agent,
    create_draft_test_snapshot,
    publish_agent,
    update_agent_draft,
)
from app.services.registry_reconcile import seed_core_registries
from app.schemas.agent_v1 import AgentStateCommand

pytestmark = pytest.mark.unit


@pytest.fixture
async def assembly_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, PlatformAuditEvent.__table__,
        IdempotencyRecord.__table__,
        RegisteredModel.__table__, PlatformTool.__table__, RegisteredSkill.__table__,
        PolicyDefinition.__table__, SandboxProfile.__table__, AgentBlueprint.__table__,
        AgentAssemblyVersion.__table__, AgentVersionTool.__table__,
        AgentVersionSkill.__table__, AgentVersionBinding.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
        # Mirror the MySQL sealed-relation guard so flush ordering is covered
        # by the fast SQLite suite as well.
        await connection.execute(text("""
            CREATE TRIGGER test_agent_version_skill_sealed_insert
            BEFORE INSERT ON agent_version_skill
            WHEN EXISTS (
                SELECT 1 FROM agent_version
                WHERE id = NEW.agent_version_id AND immutable_at IS NOT NULL
            )
            BEGIN
                SELECT RAISE(ABORT, 'sealed agent assembly is immutable');
            END
        """))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account_id = uuid7()
    model_id = uuid7()
    async with factory.begin() as db:
        db.add(UserIdentity(user_id="builder-1", status="active"))
        db.add(PlatformAccount(id=account_id, user_id="builder-1", display_name="Builder", status="active"))
        db.add(RegisteredModel(id=model_id, key="mock.default", provider="openai_compatible", model_name="mock", default_parameters={}, capabilities={}, enabled=True, config_version=1))
        await seed_core_registries(db)
    yield factory, account_id, model_id
    await engine.dispose()


async def _policy_ids(db) -> dict[str, str]:
    rows = list(await db.scalars(select(PolicyDefinition)))
    return {row.policy_key: row.id for row in rows}


@pytest.mark.asyncio
async def test_incomplete_draft_saves_but_cannot_publish(assembly_db):
    factory, account_id, _ = assembly_db
    async with factory.begin() as db:
        agent, draft = await create_agent(db, owner_account_id=account_id, created_by_account_id=account_id)
        with pytest.raises(AgentPublishValidationError) as error:
            await publish_agent(db, agent_id=agent.id, expected_draft_revision=draft.draft_revision, actor_account_id=account_id, idempotency_record_id="publish-incomplete")
        assert "name is required" in error.value.issues
        assert "model is required" in error.value.issues


@pytest.mark.asyncio
async def test_publish_copies_and_seals_while_preserving_draft(assembly_db):
    factory, account_id, model_id = assembly_db
    async with factory.begin() as db:
        policies = await _policy_ids(db)
        sandbox_id = await db.scalar(select(SandboxProfile.id).where(SandboxProfile.profile_key == "local"))
        skill_id = uuid7()
        db.add(RegisteredSkill(
            id=skill_id, logical_name="acceptance.skill", name="Acceptance Skill",
            description="Draft snapshot regression", owner_account_id=account_id,
            status="active", current_published_version_id=uuid7(),
        ))
        await db.flush()
        agent, draft = await create_agent(db, owner_account_id=account_id, created_by_account_id=account_id, name="Finance", description="Finance specialist")
        draft_id = draft.id
        draft = await update_agent_draft(
            db,
            agent_id=agent.id,
            expected_draft_revision=1,
            values={
                "instructions": "Handle finance work safely.",
                "model_id": model_id,
                "sandbox_profile_id": sandbox_id,
                "context_policy_id": policies["context.standard"],
                "history_policy_id": policies["history.recent"],
                "memory_write_policy_id": policies["memory_write.none"],
                "memory_recall_policy_id": policies["memory_recall.none"],
                "group_enabled": True,
            },
            selection=AssemblySelection(skill_ids=(skill_id,)),
        )
        test_snapshot = await create_draft_test_snapshot(
            db, agent_id=agent.id, expected_draft_revision=2,
            actor_account_id=account_id, idempotency_record_id="draft-test-1",
        )
        assert test_snapshot.publication_status == "draft_test_snapshot"
        assert test_snapshot.immutable_at is not None
        assert await db.scalar(select(AgentVersionSkill.skill_id).where(
            AgentVersionSkill.agent_version_id == test_snapshot.id,
        )) == skill_id
        assert draft.draft_revision == 2
        published = await publish_agent(db, agent_id=agent.id, expected_draft_revision=2, actor_account_id=account_id, idempotency_record_id="publish-1")
        assert published.version_no == 1
        assert published.immutable_at is not None
        assert len(published.content_hash) == 64
        assert agent.draft_version_id == draft_id
        assert draft.draft_revision == 3
        assert draft.derived_from_version_id == published.id
        assert agent.current_published_version_id == published.id
        assert agent.status == "published"

    async with factory() as db:
        stored = await db.get(AgentAssemblyVersion, published.id)
        assert stored.instructions == "Handle finance work safely."
        assert stored.publication_status == "published"


@pytest.mark.asyncio
async def test_draft_revision_conflict_does_not_overwrite(assembly_db):
    factory, account_id, _ = assembly_db
    async with factory.begin() as db:
        agent, _ = await create_agent(db, owner_account_id=account_id, created_by_account_id=account_id)
        await update_agent_draft(db, agent_id=agent.id, expected_draft_revision=1, values={"name": "First"})
        with pytest.raises(AgentDraftConflict):
            await update_agent_draft(db, agent_id=agent.id, expected_draft_revision=1, values={"name": "Stale"})
        draft = await db.get(AgentAssemblyVersion, agent.draft_version_id)
        assert draft.name == "First"


@pytest.mark.asyncio
async def test_enterprise_user_summary_never_exposes_unpublished_draft(assembly_db):
    factory, account_id, model_id = assembly_db
    async with factory.begin() as db:
        policies = await _policy_ids(db)
        sandbox_id = await db.scalar(select(SandboxProfile.id).where(SandboxProfile.profile_key == "local"))
        agent, _ = await create_agent(
            db, owner_account_id=account_id, created_by_account_id=account_id,
            name="Published Name", visibility="enterprise",
        )
        draft = await update_agent_draft(
            db, agent_id=agent.id, expected_draft_revision=1,
            values={
                "description": "Published description",
                "instructions": "Published instructions", "model_id": model_id,
                "sandbox_profile_id": sandbox_id,
                "context_policy_id": policies["context.standard"],
                "history_policy_id": policies["history.recent"],
                "memory_write_policy_id": policies["memory_write.none"],
                "memory_recall_policy_id": policies["memory_recall.none"],
            },
        )
        await publish_agent(
            db, agent_id=agent.id, expected_draft_revision=draft.draft_revision,
            actor_account_id=account_id, idempotency_record_id="publish-public-summary",
        )
        await update_agent_draft(
            db, agent_id=agent.id, expected_draft_revision=3,
            values={"name": "Unpublished Secret Name", "group_enabled": True},
        )
        outsider = PlatformAccount(
            id=uuid7(), user_id="enterprise-user", display_name="Enterprise User", status="active",
        )
        db.add(UserIdentity(user_id="enterprise-user", status="active"))
        db.add(outsider)
        await db.flush()

        summary = await _summary(db, agent, CurrentAccount(outsider, frozenset({"user"})))
        assert summary.name == "Published Name"
        assert summary.group_enabled is False


def test_if_match_requires_a_numeric_revision():
    assert _if_match_revision('W/"draft-42"') == 42
    with pytest.raises(HTTPException) as missing:
        _if_match_revision(None)
    assert missing.value.status_code == 428
    with pytest.raises(HTTPException) as malformed:
        _if_match_revision('W/"draft-current"')
    assert malformed.value.status_code == 400


@pytest.mark.asyncio
async def test_agent_access_is_fenced_by_owner_visibility_and_publish_state(assembly_db):
    factory, owner_id, _ = assembly_db
    async with factory.begin() as db:
        agent, _ = await create_agent(
            db, owner_account_id=owner_id, created_by_account_id=owner_id,
            name="Private draft", visibility="private",
        )
        outsider = PlatformAccount(
            id=uuid7(), user_id="outsider", display_name="Outsider", status="active",
        )
        db.add(UserIdentity(user_id="outsider", status="active"))
        db.add(outsider)
        await db.flush()
        outsider_current = CurrentAccount(outsider, frozenset({"user"}))

        with pytest.raises(HTTPException) as private:
            await _get_accessible_agent(db, agent.id, outsider_current)
        assert private.value.status_code == 403
        with pytest.raises(HTTPException) as not_owner:
            await _get_owned_agent(db, agent.id, outsider_current)
        assert not_owner.value.status_code == 403

        agent.visibility = "enterprise"
        with pytest.raises(HTTPException) as unpublished:
            await _get_accessible_agent(db, agent.id, outsider_current)
        assert unpublished.value.status_code == 403

        agent.status = "published"
        assert await _get_accessible_agent(db, agent.id, outsider_current) is agent
        admin = CurrentAccount(outsider, frozenset({"admin"}))
        assert await _get_owned_agent(db, agent.id, admin) is agent

        with pytest.raises(HTTPException) as absent:
            await _get_accessible_agent(db, "missing", outsider_current)
        assert absent.value.status_code == 404


@pytest.mark.asyncio
async def test_idempotency_replays_exact_request_and_rejects_mismatch(assembly_db):
    factory, account_id, _ = assembly_db
    async with factory.begin() as db:
        account = await db.get(PlatformAccount, account_id)
        current = CurrentAccount(account, frozenset({"builder"}))
        first, replay = await _idempotency_record(
            db, current=current, operation="test", key="same-key", request={"value": 1},
        )
        assert replay is False
        second, replay = await _idempotency_record(
            db, current=current, operation="test", key="same-key", request={"value": 1},
        )
        assert replay is True
        assert second.id == first.id
        with pytest.raises(HTTPException) as mismatch:
            await _idempotency_record(
                db, current=current, operation="test", key="same-key", request={"value": 2},
            )
        assert mismatch.value.status_code == 409


@pytest.mark.asyncio
async def test_agent_state_replay_is_stable_and_failed_reactivate_leaves_no_record(assembly_db):
    factory, account_id, _ = assembly_db
    async with factory() as db:
        async with db.begin():
            account = await db.get(PlatformAccount, account_id)
            current = CurrentAccount(account, frozenset({"builder"}))
            agent, _ = await create_agent(
                db, owner_account_id=account_id, created_by_account_id=account_id,
                name="Lifecycle",
            )
            agent_id = agent.id

        with pytest.raises(HTTPException) as rejected:
            await _change_agent_state(
                agent_id=agent_id, target="published", body=AgentStateCommand(),
                idempotency_key="reactivate-without-version", db=db, current=current,
            )
        assert rejected.value.status_code == 409
        assert await db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.idempotency_key == "reactivate-without-version"
        )) is None
        await db.rollback()
        account = await db.get(PlatformAccount, account_id)
        current = CurrentAccount(account, frozenset({"builder"}))

        first = await _change_agent_state(
            agent_id=agent_id, target="inactive", body=AgentStateCommand(reason="maintenance"),
            idempotency_key="deactivate-once", db=db, current=current,
        )
        first_version = first.version
        replay = await _change_agent_state(
            agent_id=agent_id, target="inactive", body=AgentStateCommand(reason="maintenance"),
            idempotency_key="deactivate-once", db=db, current=current,
        )
        assert replay.status == "inactive"
        assert replay.version == first_version
        stored = await db.get(AgentBlueprint, agent_id)
        assert stored.version == first_version
