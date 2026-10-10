"""Context projections cannot turn Session ownership into Agent visibility."""

from datetime import timedelta
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.artifact_v1 import Artifact, GroupArtifact, MessageArtifact
from app.models.common import utcnow, uuid7
from app.models.foundation import UserSession
from app.models.group_v1 import GroupMembership, GroupRun, GroupRunMember, GroupRunReference, GroupSession
from app.models.memory_v1 import MemoryHead, MemoryRecord, RunnerMemorySnapshot
from app.models.runtime_v1 import DurableRunner, Message, Run, WorkItem
from app.models.tenant_v1 import DEFAULT_TENANT_ID
from app.services.context_service import assemble_standard_context
from app.services.memory_service import normalized_content, recall_memories

pytestmark = pytest.mark.unit


@pytest.fixture
async def context_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory.begin() as db:
            yield db
    finally:
        await engine.dispose()


async def _runner(db, *, mode="chat", delegated=False):
    session = UserSession(id=uuid7(), user_id="owner", entry_agent_id="agent-a", status="active")
    run = Run(id=uuid7(), session_id=session.id, user_id="owner", mode=mode, status="active")
    work = WorkItem(
        id=uuid7(), run_id=run.id, session_id=session.id, user_id="owner", mode=mode,
        owner_agent_id="agent-a", agent_version_id="version-a", goal="Select visible metadata",
        status="running", execution_epoch=1, attempt_no=1,
    )
    runner = DurableRunner(
        id=uuid7(), work_item_id=work.id, run_id=run.id, session_id=session.id,
        agent_id="agent-a", agent_version_id="version-a", execution_epoch=1, status="running",
        principal_snapshot={}, assembly_snapshot={}, prompt_manifest={},
        input_snapshot={"agent_invocation_id": "parent-call"} if delegated else {},
    )
    db.add_all([session, run, work, runner])
    result = SimpleNamespace(session=session, run=run, work=work, runner=runner)
    if mode == "group":
        group = GroupSession(
            id=uuid7(), session_id=session.id, user_id="owner", initiator_agent_id="agent-a",
        )
        group_run = GroupRun(id=uuid7(), run_id=run.id, group_session_id=group.id, goal="Group goal")
        membership = GroupMembership(id=uuid7(), group_session_id=group.id, agent_id="agent-a")
        db.add_all([group, group_run, membership, GroupRunMember(
            group_run_id=group_run.id, agent_id="agent-a", membership_id=membership.id,
            agent_version_id="version-a",
        )])
        work.group_run_id, work.group_membership_id = group_run.id, membership.id
        runner.input_snapshot = {**runner.input_snapshot, "group_run_id": group_run.id,
                                 "group_membership_id": membership.id, "group_session_id": group.id}
        result.group, result.group_run, result.membership = group, group_run, membership
    await db.flush()
    return result


def _artifact(db, state, *, agent="agent-a", **overrides):
    identity = uuid7()
    row = Artifact(**{
        "id": identity, "user_id": "owner", "session_id": state.session.id,
        "workspace_id": "workspace", "relative_path": f"private/{identity}.txt",
        "name": f"{identity}.txt", "mime_type": "text/plain", "size_bytes": 5,
        "checksum": "a" * 64, "publication_key": identity.replace("-", "").ljust(64, "0"),
        "created_by_agent_id": agent, "status": "published", "version_no": 1,
        "created_at": utcnow(), **overrides,
    })
    db.add(row)
    return row


def _attachment(db, state, artifact, *, scope="chat", visibility="user", **overrides):
    values = {
        "id": uuid7(), "session_id": state.session.id, "user_id": "owner",
        "scope_type": scope, "sender_type": "agent", "sender_agent_id": "agent-b",
        "recipient_type": "session", "role": "assistant", "content": "Explicit delivery",
        "visibility": visibility, "status": "committed",
    }
    if scope == "group" and hasattr(state, "group"):
        values.update(group_session_id=state.group.id, group_run_id=state.group_run.id)
    message = Message(**{**values, **overrides})
    db.add_all([message, MessageArtifact(message_id=message.id, artifact_id=artifact.id, visibility=visibility)])
    return message


async def _selected(db, state, limit=20):
    await db.flush()
    selected = await assemble_standard_context(
        db, run=state.run, runner=state.runner, policy=None,
        parameters={"max_artifacts": limit, "token_budget": 8000},
    )
    items = json.loads(selected.content)["published_artifacts"]
    ids = {item["artifact_id"] for item in items}
    assert ids == set(selected.manifest["source_ids"]["artifact_ids"])
    assert all(set(item) == {"artifact_id", "name", "mime_type", "checksum", "version_no", "parent_artifact_id"}
               for item in items)
    return ids


@pytest.mark.asyncio
async def test_chat_root_keeps_own_and_delivered_metadata_without_other_agent_inventory(context_db):
    db = context_db
    state = await _runner(db)
    own = _artifact(db, state)
    delivered = _artifact(db, state, agent="agent-b")
    _attachment(db, state, delivered)
    hidden = _artifact(db, state, agent="agent-b")
    redacted = _artifact(db, state, agent="agent-b")
    _attachment(db, state, redacted, status="redacted")
    for overrides in ({"status": "deleted"}, {"session_id": uuid7()}, {"user_id": "someone-else"},
                      {"tenant_id": uuid7()}):
        _artifact(db, state, **overrides)
    assert await _selected(db, state) == {own.id, delivered.id}
    assert hidden.id not in await _selected(db, state)


@pytest.mark.asyncio
async def test_delegated_agent_does_not_inherit_parent_artifacts_or_attachments(context_db):
    db = context_db
    state = await _runner(db, delegated=True)
    own = _artifact(db, state)
    parent = _artifact(db, state, agent="parent-agent")
    _attachment(db, state, parent)
    # A reference in freeform invocation context is not an ambient metadata grant.
    state.runner.input_snapshot = {**state.runner.input_snapshot, "context": {"artifact_id": parent.id}}
    assert await _selected(db, state) == {own.id}


@pytest.mark.asyncio
async def test_group_metadata_requires_direct_delivery_and_current_history_scope(context_db):
    db = context_db
    state = await _runner(db, mode="group")
    own = _artifact(db, state)
    direct = _artifact(db, state, agent="agent-b")
    _attachment(db, state, direct, scope="group", visibility="direct",
                recipient_type="agent", recipient_agent_id="agent-a", recipient_membership_id=state.membership.id)
    for kwargs in (
        {"visibility": "direct", "recipient_agent_id": "agent-c"},
        {"visibility": "group", "recipient_type": "group"},
        {"visibility": "user", "recipient_agent_id": "agent-a"},
    ):
        row = _artifact(db, state, agent="agent-b")
        _attachment(db, state, row, scope="group", **kwargs)
    # Group-catalog publication remains explicitly queryable, not ambient input.
    catalog = _artifact(db, state, agent="agent-b")
    db.add(GroupArtifact(group_session_id=state.group.id, artifact_id=catalog.id,
                         published_by_membership_id="membership-b"))
    old_run = GroupRun(id=uuid7(), run_id=uuid7(), group_session_id=state.group.id, goal="Earlier run")
    db.add(old_run)
    old = _artifact(db, state, agent="agent-b")
    _attachment(db, state, old, scope="group", visibility="direct", group_run_id=old_run.id,
                recipient_type="agent", recipient_agent_id="agent-a")
    assert await _selected(db, state) == {own.id, direct.id}
    db.add(GroupRunReference(group_run_id=state.group_run.id, referenced_group_run_id=old_run.id))
    assert await _selected(db, state) == {own.id, direct.id, old.id}


@pytest.mark.asyncio
async def test_hidden_attachments_do_not_consume_metadata_quota(context_db):
    db = context_db
    state = await _runner(db)
    own = _artifact(db, state, created_at=utcnow() - timedelta(days=1))
    for _ in range(105):
        hidden = _artifact(db, state, agent="agent-b")
        _attachment(db, state, hidden, scope="group", visibility="direct", recipient_agent_id="agent-c")
    assert await _selected(db, state, limit=1) == {own.id}


@pytest.mark.asyncio
async def test_context_rejects_mismatched_work_and_group_principal(context_db):
    db = context_db
    state = await _runner(db, mode="group")
    state.runner.input_snapshot = {**state.runner.input_snapshot, "group_membership_id": "someone-else"}
    with pytest.raises(RuntimeError, match="Group principal"):
        await _selected(db, state)
    state.work.owner_agent_id = "agent-b"
    with pytest.raises(RuntimeError, match="Context source"):
        await _selected(db, state)


def _memory(db, *, content="A remembered fact", **overrides):
    identity = uuid7()
    text, digest = normalized_content(content)
    values = {"tenant_id": DEFAULT_TENANT_ID, "user_id": "owner", "agent_id": "agent-a",
              "scope": "user_agent", "fact_key": identity, **overrides}
    head = MemoryHead(id=uuid7(), **{key: values[key] for key in (
        "tenant_id", "user_id", "agent_id", "scope", "fact_key",
    )})
    record = MemoryRecord(id=identity, head_id=head.id, content=text, normalized_hash=digest,
                          status="active", source_kind="import", **values)
    db.add_all([head, record])
    return record


async def _recall(db, runner_id, policy="memory_recall.user_combined"):
    await db.flush()
    return await recall_memories(db, tenant_id=DEFAULT_TENANT_ID, runner_id=runner_id,
                                 user_id="owner", agent_id="agent-a", policy_key=policy)


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["deleted", "superseded"])
async def test_same_runner_drops_revoked_memory_without_backfilling_frozen_selection(context_db, revocation):
    db = context_db
    state = await _runner(db)
    revoked = _memory(db)
    retained = _memory(db, agent_id=None, scope="user", content="Shared user fact")
    initial = await _recall(db, state.runner.id)
    assert [row.id for row in initial] == [revoked.id, retained.id]
    revoked.status = revocation
    new = _memory(db, content="New fact after selection")
    assert [row.id for row in await _recall(db, state.runner.id)] == [retained.id]
    snapshots = list(await db.scalars(select(RunnerMemorySnapshot).where(
        RunnerMemorySnapshot.runner_id == state.runner.id,
    )))
    assert {row.memory_id for row in snapshots} == {revoked.id, retained.id}
    assert {row.id for row in await _recall(db, uuid7())} == {retained.id, new.id}


@pytest.mark.asyncio
async def test_snapshot_recall_rechecks_scope_hash_and_disabled_policy(context_db):
    db = context_db
    state = await _runner(db)
    valid_agent = _memory(db)
    valid_user = _memory(db, scope="user", agent_id=None)
    rejected = [
        (_memory(db, agent_id="other-agent"), {}),
        (_memory(db, user_id="other-user"), {}),
        (_memory(db, tenant_id=uuid7()), {}),
        (_memory(db), {"content_hash": "0" * 64}),
        (_memory(db), {"scope": "user"}),
    ]
    for rank, (memory, overrides) in enumerate([*rejected, (valid_user, {}), (valid_agent, {})], 1):
        db.add(RunnerMemorySnapshot(**{
            "runner_id": state.runner.id, "memory_id": memory.id,
            "content_hash": memory.normalized_hash, "scope": memory.scope,
            "rank": rank, "score": Decimal("1.0"), **overrides,
        }))
    assert [row.id for row in await _recall(db, state.runner.id)] == [valid_user.id, valid_agent.id]
    assert [row.id for row in await _recall(db, state.runner.id, "memory_recall.user_agent")] == [valid_agent.id]
    assert await _recall(db, state.runner.id, "memory_recall.none") == []
    assert await _recall(db, state.runner.id, "unknown-policy") == []
