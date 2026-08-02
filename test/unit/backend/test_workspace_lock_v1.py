"""Fair Workspace read/write locking and writer fencing tests."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, UserIdentity, UserSession
from app.models.registry import PlatformTool, SandboxProfile
from app.models.runtime_v1 import (
    DurableRunner, Run, SessionSandbox, ToolInvocation, WorkItem, Workspace,
    WorkspaceLock, WorkspaceLockRequest, WorkspaceReadLease,
)
from app.services.workspace_lock_service import (
    WorkspaceLockConflict, acquire_workspace_lock, release_workspace_lock,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_workspace_lock_is_writer_fair_and_fenced():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, PlatformAccount.__table__, SandboxProfile.__table__,
        PlatformTool.__table__, AgentBlueprint.__table__, AgentAssemblyVersion.__table__,
        UserSession.__table__, Run.__table__, WorkItem.__table__, DurableRunner.__table__,
        SessionSandbox.__table__, Workspace.__table__, ToolInvocation.__table__,
        WorkspaceLock.__table__, WorkspaceLockRequest.__table__, WorkspaceReadLease.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids = {key: uuid7() for key in (
        "account", "agent", "version", "profile", "session", "run", "work", "runner",
        "sandbox", "workspace", "tool",
    )}
    invocation_ids = [uuid7() for _ in range(4)]
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="u1", status="active"),
            PlatformAccount(id=ids["account"], user_id="u1", display_name="U", status="active"),
            SandboxProfile(
                id=ids["profile"], profile_key="local-test", type="local", description="D",
                implementation_ref="test", config={}, config_version=1, enabled=True,
            ),
            PlatformTool(
                id=ids["tool"], tool_key="workspace-test", display_name="Workspace Test",
                description="D", input_schema={}, implementation_ref="test", implementation_version="1",
                execution_mode="inline", enabled=True, confirmation_policy="never",
                platform_config={"workspace_access": "write"}, config_version=1,
                registration_hash="a" * 64,
            ),
            AgentBlueprint(
                id=ids["agent"], owner_account_id=ids["account"], visibility="enterprise",
                status="published", current_published_version_id=ids["version"],
                draft_version_id=uuid7(), next_version_no=2,
            ),
            AgentAssemblyVersion(
                id=ids["version"], agent_id=ids["agent"], version_no=1,
                publication_status="published", name="A", description="D", instructions="I",
                group_enabled=False, created_by_account_id=ids["account"],
                published_at=utcnow(), immutable_at=utcnow(), content_hash="b" * 64,
            ),
            UserSession(
                id=ids["session"], user_id="u1", entry_agent_id=ids["agent"],
                status="active", origin_type="web", next_event_sequence=1,
            ),
            Run(
                id=ids["run"], session_id=ids["session"], user_id="u1", mode="chat",
                execution_kind="normal", lifecycle="running", entry_agent_id=ids["agent"],
                started_at=utcnow(),
            ),
            WorkItem(
                id=ids["work"], run_id=ids["run"], session_id=ids["session"], user_id="u1",
                mode="chat", owner_agent_id=ids["agent"], agent_version_id=ids["version"],
                goal="work", status="running", execution_epoch=1, attempt_no=1,
                active_runner_id=ids["runner"],
            ),
            DurableRunner(
                id=ids["runner"], work_item_id=ids["work"], run_id=ids["run"],
                session_id=ids["session"], agent_id=ids["agent"], agent_version_id=ids["version"],
                execution_epoch=1, status="running", principal_snapshot={}, assembly_snapshot={},
                prompt_manifest={}, input_snapshot={},
            ),
            SessionSandbox(
                id=ids["sandbox"], session_id=ids["session"], sandbox_profile_id=ids["profile"],
                profile_config_version=1, status="ready", root_uri="workspace://test/",
            ),
            Workspace(
                id=ids["workspace"], session_sandbox_id=ids["sandbox"], session_id=ids["session"],
                root_uri="workspace://test/", status="active", retention_policy_key="session_lifetime",
            ),
        ])
        for index, invocation_id in enumerate(invocation_ids):
            db.add(ToolInvocation(
                id=invocation_id, runner_id=ids["runner"], work_item_id=ids["work"],
                run_id=ids["run"], session_id=ids["session"], platform_tool_id=ids["tool"],
                tool_key="workspace-test", execution_mode="inline", status="queued", arguments={},
                arguments_hash=str(index) * 64, business_idempotency_key=f"call-{index}",
                principal_hash="p" * 64, attempt_count=0,
            ))

    async with factory.begin() as db:
        read1 = await acquire_workspace_lock(
            db, workspace_id=ids["workspace"], tool_invocation_id=invocation_ids[0], mode="read",
        )
        write1 = await acquire_workspace_lock(
            db, workspace_id=ids["workspace"], tool_invocation_id=invocation_ids[1], mode="write",
        )
        read2 = await acquire_workspace_lock(
            db, workspace_id=ids["workspace"], tool_invocation_id=invocation_ids[2], mode="read",
        )
        assert read1.status == "granted"
        assert write1.status == "queued"
        assert read2.status == "queued"
        _, granted = await release_workspace_lock(
            db, request_id=read1.request_id, lease_token=read1.lease_token,
        )
        assert [item.request_id for item in granted] == [write1.request_id]
        write1 = granted[0]
        assert write1.fencing_token == 1
        with pytest.raises(WorkspaceLockConflict):
            await release_workspace_lock(
                db, request_id=write1.request_id, lease_token=write1.lease_token,
                fencing_token=0,
            )
        _, granted = await release_workspace_lock(
            db, request_id=write1.request_id, lease_token=write1.lease_token,
            fencing_token=write1.fencing_token,
        )
        assert [item.request_id for item in granted] == [read2.request_id]
        read2 = granted[0]
        await release_workspace_lock(
            db, request_id=read2.request_id, lease_token=read2.lease_token,
        )
        write2 = await acquire_workspace_lock(
            db, workspace_id=ids["workspace"], tool_invocation_id=invocation_ids[3], mode="write",
        )
        assert write2.status == "granted" and write2.fencing_token == 2
    await engine.dispose()
