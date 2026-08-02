"""Artifact publication is explicit, idempotent, checksummed, and Workspace-backed."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.tool_registry.base import RuntimeContext
from app.core.tool_registry.registry import ToolRegistry
from app.database import Base
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.artifact_v1 import Artifact
from app.models.common import utcnow, uuid7
from app.models.foundation import ExecutionEvent, ExecutionEventRecipient, PlatformAccount, UserIdentity, UserSession
from app.models.registry import PlatformTool, SandboxProfile
from app.models.runtime_v1 import (
    DurableRunner, Run, SessionSandbox, ToolInvocation, WorkItem, Workspace,
    WorkspaceLock, WorkspaceLockRequest, WorkspaceReadLease,
)
from app.services.workspace_lock_service import acquire_workspace_lock, release_workspace_lock

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_publish_artifact_copies_to_immutable_namespace_and_replays(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_root", str(tmp_path))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, SandboxProfile.__table__,
        PlatformTool.__table__, AgentBlueprint.__table__, AgentAssemblyVersion.__table__,
        UserSession.__table__, Run.__table__, WorkItem.__table__, DurableRunner.__table__,
        SessionSandbox.__table__, Workspace.__table__, ToolInvocation.__table__,
        WorkspaceLock.__table__, WorkspaceLockRequest.__table__, WorkspaceReadLease.__table__,
        Artifact.__table__, ExecutionEvent.__table__, ExecutionEventRecipient.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids = {key: uuid7() for key in (
        "account", "agent", "version", "profile", "tool", "session", "run", "work",
        "runner", "sandbox", "workspace", "invocation",
    )}
    root = tmp_path / ids["session"]
    root.mkdir()
    (root / "draft.txt").write_text("formal result")
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="u1", status="active"),
            PlatformAccount(id=ids["account"], user_id="u1", display_name="U", status="active"),
            SandboxProfile(id=ids["profile"], profile_key="artifact-test", type="local",
                description="D", implementation_ref="test", config={}, config_version=1, enabled=True),
            PlatformTool(id=ids["tool"], tool_key="publish_artifact", display_name="Publish",
                description="D", input_schema={}, implementation_ref="test", implementation_version="1",
                execution_mode="inline", enabled=True, confirmation_policy="always",
                platform_config={"workspace_access": "write"}, config_version=1, registration_hash="a" * 64),
            AgentBlueprint(id=ids["agent"], owner_account_id=ids["account"], visibility="enterprise",
                status="published", current_published_version_id=ids["version"],
                draft_version_id=uuid7(), next_version_no=2),
            AgentAssemblyVersion(id=ids["version"], agent_id=ids["agent"], version_no=1,
                publication_status="published", name="A", description="D", instructions="I",
                group_enabled=False, created_by_account_id=ids["account"], immutable_at=utcnow(),
                published_at=utcnow(), content_hash="b" * 64),
            UserSession(id=ids["session"], user_id="u1", entry_agent_id=ids["agent"],
                status="active", origin_type="web", next_event_sequence=1),
            Run(id=ids["run"], session_id=ids["session"], user_id="u1", mode="chat",
                execution_kind="normal", lifecycle="running", entry_agent_id=ids["agent"], started_at=utcnow()),
            WorkItem(id=ids["work"], run_id=ids["run"], session_id=ids["session"], user_id="u1",
                mode="chat", owner_agent_id=ids["agent"], agent_version_id=ids["version"], goal="G",
                status="running", execution_epoch=1, attempt_no=1, active_runner_id=ids["runner"]),
            DurableRunner(id=ids["runner"], work_item_id=ids["work"], run_id=ids["run"],
                session_id=ids["session"], agent_id=ids["agent"], agent_version_id=ids["version"],
                execution_epoch=1, status="running", principal_snapshot={}, assembly_snapshot={},
                prompt_manifest={}, input_snapshot={}),
            SessionSandbox(id=ids["sandbox"], session_id=ids["session"], sandbox_profile_id=ids["profile"],
                profile_config_version=1, status="ready", root_uri="workspace://test/"),
            Workspace(id=ids["workspace"], session_sandbox_id=ids["sandbox"], session_id=ids["session"],
                root_uri="workspace://test/", status="active", retention_policy_key="session_lifetime"),
            ToolInvocation(id=ids["invocation"], runner_id=ids["runner"], work_item_id=ids["work"],
                run_id=ids["run"], session_id=ids["session"], platform_tool_id=ids["tool"],
                tool_key="publish_artifact", execution_mode="inline", status="running",
                arguments={"path": "draft.txt"}, arguments_hash="c" * 64,
                business_idempotency_key="publish-1", principal_hash="d" * 64, attempt_count=1),
        ])
    async with factory.begin() as db:
        grant = await acquire_workspace_lock(db, workspace_id=ids["workspace"],
            tool_invocation_id=ids["invocation"], mode="write")
    context = RuntimeContext(session_id=ids["session"], runner_id=ids["runner"], task_id=ids["work"],
        user_id="u1", agent_id=ids["agent"], workspace_id=ids["workspace"],
        workspace_lock_request_id=grant.request_id, workspace_lease_token=grant.lease_token,
        workspace_fencing_token=grant.fencing_token, tool_invocation_id=ids["invocation"])
    async with factory() as db:
        first = await ToolRegistry.execute("publish_artifact", {"path": "draft.txt"}, context, db=db)
        await db.commit()
    assert first.success is True
    artifact_id = first.data["artifact"]["id"]
    assert (root / ".myclaw" / "artifacts" / artifact_id / "draft.txt").read_text() == "formal result"
    (root / "draft.txt").write_text("later mutable edit")
    assert (root / ".myclaw" / "artifacts" / artifact_id / "draft.txt").read_text() == "formal result"
    async with factory() as db:
        copied = await ToolRegistry.execute("write_file", {
            "path": "restored/from-artifact.txt", "source_artifact_id": artifact_id, "mode": "create",
        }, context, db=db)
        assert copied.success is True
        assert copied.data["source_artifact_id"] == artifact_id
    assert (root / "restored" / "from-artifact.txt").read_text() == "formal result"
    async with factory() as db:
        replay = await ToolRegistry.execute("publish_artifact", {"path": "draft.txt"}, context, db=db)
        assert replay.data["artifact"]["id"] == artifact_id
        assert await db.scalar(select(func.count()).select_from(Artifact)) == 1
        event = await db.scalar(select(ExecutionEvent).where(ExecutionEvent.entity_id == artifact_id))
        assert event.event_type == "artifact.published" and event.visibility == "user"
    async with factory.begin() as db:
        await release_workspace_lock(db, request_id=grant.request_id,
            lease_token=grant.lease_token, fencing_token=grant.fencing_token)
    await engine.dispose()
