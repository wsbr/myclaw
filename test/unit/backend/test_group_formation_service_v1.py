from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.common import utcnow
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.foundation import PlatformAccount, UserIdentity
from app.models.group_v1 import (
    GroupFormationParticipant, GroupFormationProposal, GroupFormationRevision,
    GroupFormationSuggestedEdge, GroupFormationWorkItem, GroupSession,
)
from app.models.runtime_v1 import Run
from app.schemas.group_v1 import (
    GroupInitialWorkItemInput, GroupParticipantInput, GroupSuggestedEdgeInput,
)
from app.services.group_formation_service import (
    GroupFormationValidationError, create_revision, proposal_view, validate_formation,
)


pytestmark = pytest.mark.unit


@pytest.fixture
async def formation_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, AgentBlueprint.__table__,
        AgentAssemblyVersion.__table__, Run.__table__, GroupSession.__table__,
        GroupFormationProposal.__table__, GroupFormationRevision.__table__,
        GroupFormationParticipant.__table__, GroupFormationWorkItem.__table__,
        GroupFormationSuggestedEdge.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    entry_id, entry_version, peer_id, peer_version = "entry", "entry-v1", "peer", "peer-v1"
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="user-1", status="active"),
            PlatformAccount(id="account-1", user_id="user-1", display_name="U", status="active"),
            AgentBlueprint(id=entry_id, owner_account_id="account-1", visibility="enterprise",
                status="published", current_published_version_id=entry_version,
                draft_version_id="entry-draft", next_version_no=2),
            AgentBlueprint(id=peer_id, owner_account_id="account-1", visibility="enterprise",
                status="published", current_published_version_id=peer_version,
                draft_version_id="peer-draft", next_version_no=2),
            AgentAssemblyVersion(id=entry_version, agent_id=entry_id, version_no=1,
                publication_status="published", name="Entry", description="", instructions="",
                group_enabled=True, created_by_account_id="account-1", immutable_at=utcnow()),
            AgentAssemblyVersion(id=peer_version, agent_id=peer_id, version_no=1,
                publication_status="published", name="Peer", description="", instructions="",
                group_enabled=True, created_by_account_id="account-1", immutable_at=utcnow()),
            GroupSession(id="group", parent_session_id="session", user_id="user-1",
                entry_agent_id=entry_id, status="forming"),
            Run(id="generation-run", session_id="session", user_id="user-1", mode="group",
                execution_kind="proposal_generation", lifecycle="running", entry_agent_id=entry_id,
                started_at=utcnow()),
            GroupFormationProposal(id="proposal", parent_session_id="session",
                group_session_id="group", user_id="user-1", entry_agent_id=entry_id,
                generation_run_id="generation-run", status="generating", current_revision_no=0),
        ])
    yield factory
    await engine.dispose()


def _participants() -> list[GroupParticipantInput]:
    return [
        GroupParticipantInput(participant_key="entry", agent_id="entry", is_entry=True,
            initial_responsibility="Own"),
        GroupParticipantInput(participant_key="peer", agent_id="peer", is_entry=False,
            initial_responsibility="Support"),
    ]


def _work() -> list[GroupInitialWorkItemInput]:
    return [GroupInitialWorkItemInput(work_item_key="w1", participant_key="entry",
        title="Work", goal="Deliver", acceptance_criteria="Evidence")]


@pytest.mark.asyncio
async def test_valid_formation_resolves_frozen_versions(formation_db):
    async with formation_db() as db:
        proposal = await db.get(GroupFormationProposal, "proposal")
        versions = await validate_formation(
            db, proposal, _participants(), _work(), [], resolve_versions=True,
        )
    assert versions == {"entry": "entry-v1", "peer": "peer-v1"}


@pytest.mark.asyncio
async def test_structural_validation_reports_all_graph_errors(formation_db):
    participants = [
        GroupParticipantInput(participant_key="dup", agent_id="entry", is_entry=False,
            initial_responsibility="A"),
        GroupParticipantInput(participant_key="dup", agent_id="entry", is_entry=False,
            initial_responsibility="B"),
    ]
    work = [
        GroupInitialWorkItemInput(work_item_key="same", participant_key="missing", title="A",
            goal="A", acceptance_criteria="A"),
        GroupInitialWorkItemInput(work_item_key="same", participant_key="missing", title="B",
            goal="B", acceptance_criteria="B"),
    ]
    edges = [
        GroupSuggestedEdgeInput(source_participant_key="dup", target_participant_key="dup",
            label="self", rationale=""),
        GroupSuggestedEdgeInput(source_participant_key="dup", target_participant_key="dup",
            label="duplicate", rationale=""),
        GroupSuggestedEdgeInput(source_participant_key="missing", target_participant_key="dup",
            label="unknown", rationale=""),
    ]
    async with formation_db() as db:
        proposal = await db.get(GroupFormationProposal, "proposal")
        with pytest.raises(GroupFormationValidationError) as captured:
            await validate_formation(db, proposal, participants, work, edges)
    issues = captured.value.issues
    assert any("participant_key" in issue for issue in issues)
    assert any("participant Agents" in issue for issue in issues)
    assert any("entry participant" in issue for issue in issues)
    assert any("work_item_key" in issue for issue in issues)
    assert any("unknown Work Item participant" in issue for issue in issues)
    assert any("self edge" in issue for issue in issues)
    assert any("duplicate suggested edge" in issue for issue in issues)
    assert any("unknown participant" in issue for issue in issues)


@pytest.mark.asyncio
async def test_unavailable_private_and_group_disabled_agents_are_rejected(formation_db):
    async with formation_db.begin() as db:
        entry = await db.get(AgentBlueprint, "entry")
        entry.status = "inactive"
        peer = await db.get(AgentBlueprint, "peer")
        peer.visibility = "private"
        peer.owner_account_id = "different-owner"
    async with formation_db() as db:
        proposal = await db.get(GroupFormationProposal, "proposal")
        with pytest.raises(GroupFormationValidationError) as captured:
            await validate_formation(db, proposal, _participants(), _work(), [])
    assert any("unavailable: entry" in issue for issue in captured.value.issues)
    assert any("not accessible" in issue for issue in captured.value.issues)

    async with formation_db.begin() as db:
        entry = await db.get(AgentBlueprint, "entry")
        entry.status = "published"
        version = await db.get(AgentAssemblyVersion, "entry-v1")
        version.group_enabled = False
    async with formation_db() as db:
        proposal = await db.get(GroupFormationProposal, "proposal")
        with pytest.raises(GroupFormationValidationError, match="not enabled for Group"):
            await validate_formation(db, proposal, [_participants()[0]], _work(), [])


@pytest.mark.asyncio
async def test_create_revision_is_immutable_relational_and_updates_current_pointer(formation_db):
    edges = [GroupSuggestedEdgeInput(
        source_participant_key="entry", target_participant_key="peer",
        label=" Consult ", rationale=" Evidence ",
    )]
    async with formation_db.begin() as db:
        proposal = await db.get(GroupFormationProposal, "proposal")
        revision = await create_revision(
            db, proposal, goal=" Goal ", bootstrap_context=" Context ",
            participants=_participants(), work_items=_work(), edges=edges,
            source="generated", edited_by_type="agent", edited_by_id="entry",
        )
        revision_id = revision.id
    async with formation_db() as db:
        proposal = await db.get(GroupFormationProposal, "proposal")
        view = await proposal_view(db, proposal)
        assert view["status"] == "awaiting_confirmation"
        assert view["current_revision_no"] == 1
        assert view["current_revision"]["id"] == revision_id
        assert view["current_revision"]["goal"] == "Goal"
        assert view["current_revision"]["suggested_edges"][0]["label"] == "Consult"
        assert await db.scalar(select(func.count()).select_from(GroupFormationParticipant)) == 2
        assert await db.scalar(select(func.count()).select_from(GroupFormationWorkItem)) == 1
        assert await db.scalar(select(func.count()).select_from(GroupFormationSuggestedEdge)) == 1
