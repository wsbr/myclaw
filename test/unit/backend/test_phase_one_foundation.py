"""Regression tests for the phase-one reliable persistence foundation."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.common import uuid7
from app.models.foundation import (
    ExecutionEvent,
    ExecutionEventRecipient,
    OutboxCommand,
    PlatformAuditEvent,
    UserIdentity,
    UserSession,
)
from app.models.registry import PlatformTool, PolicyDefinition, SandboxProfile
from app.persistence.reliable_writes import ExecutionEventWriter, OutboxWriter
from app.persistence.reliable_writes import EventRecipient
from app.services.visible_events import VisibleEventProjection
from app.services.registry_reconcile import (
    CodeToolSpec,
    reconcile_platform_tools,
    seed_core_registries,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__,
        UserSession.__table__,
        ExecutionEvent.__table__,
        ExecutionEventRecipient.__table__,
        OutboxCommand.__table__,
        PlatformAuditEvent.__table__,
        PlatformTool.__table__,
        PolicyDefinition.__table__,
        SandboxProfile.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _create_session(factory) -> str:
    session_id = uuid7()
    async with factory.begin() as db:
        db.add(UserIdentity(user_id="u-1", status="active"))
        db.add(
            UserSession(
                id=session_id,
                user_id="u-1",
                entry_agent_id=uuid7(),
                status="active",
                origin_type="web",
            )
        )
    return session_id


@pytest.mark.asyncio
async def test_event_and_outbox_roll_back_together(session_factory):
    session_id = await _create_session(session_factory)

    with pytest.raises(RuntimeError):
        async with session_factory.begin() as db:
            event = await ExecutionEventWriter(db).append(
                session_id=session_id,
                event_type="run.created",
                event_dedupe_key="transition:run:r1:0:1:create",
                actor_type="user",
                actor_id="u-1",
                entity_type="run",
                entity_id=uuid7(),
                visibility="user",
                summary="Run created",
                correlation_id="request-1",
            )
            await OutboxWriter(db).enqueue(
                topic="runtime.runner",
                action="start",
                aggregate_type="run",
                aggregate_id=event.entity_id,
                partition_key=session_id,
                dedupe_key=f"event:{event.id}:start",
            )
            raise RuntimeError("simulate domain rollback")

    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(ExecutionEvent)) == 0
        assert await db.scalar(select(func.count()).select_from(OutboxCommand)) == 0
        aggregate = await db.get(UserSession, session_id)
        assert aggregate.next_event_sequence == 1


@pytest.mark.asyncio
async def test_event_dedupe_is_idempotent_and_direct_requires_recipient(session_factory):
    session_id = await _create_session(session_factory)
    args = dict(
        session_id=session_id,
        event_type="message.created",
        event_dedupe_key="command:message:create:1",
        actor_type="user",
        actor_id="u-1",
        entity_type="message",
        entity_id=uuid7(),
        visibility="user",
        summary="Message created",
        correlation_id="request-2",
    )
    async with session_factory.begin() as db:
        first = await ExecutionEventWriter(db).append(**args)
        second = await ExecutionEventWriter(db).append(**args)
        assert first.id == second.id
        with pytest.raises(ValueError, match="recipient"):
            await ExecutionEventWriter(db).append(
                **{**args, "event_dedupe_key": "direct:1", "visibility": "direct"}
            )


@pytest.mark.asyncio
async def test_registry_seed_and_reconcile_preserve_admin_fields(session_factory):
    spec = CodeToolSpec(
        tool_key="read_file",
        display_name="Read file",
        description="Read a workspace file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        output_schema={"type": "object"},
        implementation_ref="app.tools:ReadFile",
    )
    async with session_factory.begin() as db:
        await seed_core_registries(db)
        tools = await reconcile_platform_tools(db, [spec], job_run_id="seed-1")
        tools[0].enabled = False
        tools[0].confirmation_policy = "always"

    changed = CodeToolSpec(
        **{**spec.__dict__, "description": "Read a file in the session workspace"}
    )
    async with session_factory.begin() as db:
        tools = await reconcile_platform_tools(db, [changed], job_run_id="seed-2")
        assert tools[0].enabled is False
        assert tools[0].confirmation_policy == "always"
        assert tools[0].description == changed.description

    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(PolicyDefinition)) == 9
        assert await db.scalar(select(func.count()).select_from(SandboxProfile)) == 1
        assert await db.scalar(select(func.count()).select_from(PlatformTool)) == 1
        assert await db.scalar(select(func.count()).select_from(PlatformAuditEvent)) == 2


@pytest.mark.asyncio
async def test_registry_initial_governance_is_applied_only_on_create(session_factory):
    spec = CodeToolSpec(
        tool_key="write_file", display_name="Write file", description="Write",
        input_schema={"type": "object"}, output_schema=None,
        implementation_ref="app.tools:WriteFile",
        initial_confirmation_policy="always",
        initial_platform_config={"workspace_access": "write"},
    )
    async with session_factory.begin() as db:
        tool = (await reconcile_platform_tools(db, [spec], job_run_id="defaults-1"))[0]
        assert tool.confirmation_policy == "always"
        assert tool.platform_config == {"workspace_access": "write"}
        tool.confirmation_policy = "never"
        tool.platform_config = {"workspace_access": "none"}
    changed = CodeToolSpec(**{**spec.__dict__, "description": "Write safely"})
    async with session_factory.begin() as db:
        tool = (await reconcile_platform_tools(db, [changed], job_run_id="defaults-2"))[0]
        assert tool.confirmation_policy == "never"
        assert tool.platform_config == {"workspace_access": "none"}


def test_uuid7_has_expected_version_and_variant():
    import uuid

    value = uuid.UUID(uuid7())
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


@pytest.mark.asyncio
async def test_visible_projection_advances_over_private_sequence_holes(session_factory):
    session_id = await _create_session(session_factory)
    async with session_factory.begin() as db:
        writer = ExecutionEventWriter(db)
        common = dict(
            session_id=session_id, actor_type="service", actor_id="runtime",
            entity_type="runner", entity_id=uuid7(), correlation_id="run-1",
        )
        await writer.append(
            **common, event_type="runner.private", event_dedupe_key="private-1",
            visibility="private", private_owner=("runner", uuid7()),
            summary="Private diagnostic",
        )
        await writer.append(
            **common, event_type="question.direct", event_dedupe_key="direct-other",
            visibility="direct", recipients=[EventRecipient("user_identity", "other-user")],
            summary="Another user's direct event",
        )
        visible = await writer.append(
            **common, event_type="run.completed", event_dedupe_key="visible-1",
            visibility="user", summary="Run completed",
        )
    async with session_factory() as db:
        page = await VisibleEventProjection(db).scan_session_user(
            user_id="u-1", session_id=session_id, after_sequence=0,
        )
        assert [event.id for event in page.items] == [visible.id]
        assert page.scanned_through_sequence == 3
        assert page.session_high_watermark == 3
