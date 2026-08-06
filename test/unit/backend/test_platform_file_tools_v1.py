"""Platform file/execute tools enforce Workspace and Group visibility boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.tool_registry.base import RuntimeContext
from app.core.tool_registry.builtin.search_group_history import SearchGroupHistoryTool
from app.core.tool_registry.registry import ToolRegistry
from app.database import Base
from app.models.common import uuid7
from app.models.foundation import ExecutionEvent, ExecutionEventRecipient, UserIdentity, UserSession
from app.models.group_v1 import GroupAgentNode, GroupSession
from app.models.artifact_v1 import Artifact
from app.models.runtime_v1 import Message

pytestmark = pytest.mark.unit


class WorkspaceDb:
    async def scalar(self, _statement):
        return SimpleNamespace(id="workspace-1", session_id="session-1", status="active")


def workspace_context() -> RuntimeContext:
    return RuntimeContext(
        session_id="session-1", runner_id="runner-1", user_id="user-1",
        agent_id="agent-1", workspace_id="workspace-1",
    )


@pytest.mark.asyncio
async def test_read_write_and_execute_are_confined_to_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_root", str(tmp_path))
    root = tmp_path / "session-1"
    root.mkdir()
    private = root / "agents" / "agent-1"
    private.mkdir(parents=True)
    context, db = workspace_context(), WorkspaceDb()

    traversal = await ToolRegistry.execute("write_file",
        {"path": "../escape.txt", "content": "no", "mode": "create"}, context, db=db,
    )
    assert traversal.success is False and "parent traversal" in traversal.error

    created = await ToolRegistry.execute("write_file",
        {"path": "notes/a.txt", "content": "hello world", "mode": "create"}, context, db=db,
    )
    assert created.success is True
    conflict = await ToolRegistry.execute("write_file",
        {"path": "notes/a.txt", "content": "overwrite", "mode": "create"}, context, db=db,
    )
    assert conflict.success is False and "version conflict" in conflict.error
    page = await ToolRegistry.execute("read_file",
        {"path": "notes/a.txt", "offset": 0, "limit": 5}, context, db=db,
    )
    assert page.data["content"] == "hello" and page.data["next_offset"] == 5

    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (private / "escape").symlink_to(outside)
    symlink = await ToolRegistry.execute("read_file", {"path": "escape"}, context, db=db)
    assert symlink.success is False and "escapes" in symlink.error

    executed = await ToolRegistry.execute("execute",
        {"argv": ["/usr/bin/touch", "made.txt"]},
        context, db=db,
    )
    assert executed.success is True and executed.data["output_files"] == ["made.txt"]
    echoed = await ToolRegistry.execute("execute", {"argv": ["/bin/echo", "done"]}, context, db=db)
    assert echoed.success is True and echoed.data["stdout"] == "done\n"
    leaked_env = await ToolRegistry.execute("execute",
        {"argv": ["true"], "env": {"SECRET_KEY": "bad"}}, context, db=db,
    )
    assert leaked_env.success is False and "not allowed" in leaked_env.error


@pytest.mark.asyncio
async def test_group_history_returns_group_and_own_direct_only():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        UserIdentity.__table__, UserSession.__table__, ExecutionEvent.__table__,
        ExecutionEventRecipient.__table__, Message.__table__, GroupSession.__table__,
        GroupAgentNode.__table__, Artifact.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session_id, group_id, node_id, other_node = (uuid7() for _ in range(4))
    agent_id = uuid7()
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="u1", status="active"),
            UserSession(id=session_id, user_id="u1", entry_agent_id=agent_id,
                status="active", origin_type="web", next_event_sequence=5),
            GroupSession(id=group_id, parent_session_id=session_id, user_id="u1",
                entry_agent_id=agent_id, status="open", goal="G"),
            GroupAgentNode(id=node_id, group_session_id=group_id, agent_id=agent_id,
                source_participant_id=uuid7(), is_entry=True, initial_responsibility="R"),
            ExecutionEvent(id=uuid7(), session_id=session_id, group_session_id=group_id,
                sequence=1, event_type="shared.note", event_dedupe_key="shared-1",
                actor_type="agent", actor_id=agent_id, entity_type="note", entity_id=uuid7(),
                visibility="group", summary="shared needle", payload={}, correlation_id=group_id),
            ExecutionEvent(id=uuid7(), session_id=session_id, group_session_id=group_id,
                sequence=2, event_type="direct.note", event_dedupe_key="direct-mine",
                actor_type="agent", actor_id=agent_id, entity_type="note", entity_id=uuid7(),
                visibility="direct", summary="my needle", payload={}, correlation_id=group_id),
            ExecutionEvent(id=uuid7(), session_id=session_id, group_session_id=group_id,
                sequence=3, event_type="direct.note", event_dedupe_key="direct-other",
                actor_type="agent", actor_id=agent_id, entity_type="note", entity_id=uuid7(),
                visibility="direct", summary="other needle", payload={}, correlation_id=group_id),
            ExecutionEvent(id=uuid7(), session_id=session_id, group_session_id=group_id,
                sequence=4, event_type="shared.metric", event_dedupe_key="shared-chinese",
                actor_type="agent", actor_id=agent_id, entity_type="note", entity_id=uuid7(),
                visibility="group", summary="华东仓订单取消率由3%升至11%", payload={},
                correlation_id=group_id),
        ])
        events = list(db.new)
        mine = next(row for row in events if isinstance(row, ExecutionEvent) and row.sequence == 2)
        other = next(row for row in events if isinstance(row, ExecutionEvent) and row.sequence == 3)
        db.add_all([
            ExecutionEventRecipient(event_id=mine.id, recipient_type="group_node", recipient_id=node_id),
            ExecutionEventRecipient(event_id=other.id, recipient_type="group_node", recipient_id=other_node),
        ])
    context = RuntimeContext(
        session_id=session_id, runner_id=uuid7(), user_id="u1", agent_id=agent_id,
        group_session_id=group_id, group_node_id=node_id,
    )
    async with factory() as db:
        result = await SearchGroupHistoryTool().execute({"query": "needle"}, context, db=db)
    assert result.success is True
    assert [hit["sequence"] for hit in result.data["hits"]] == [1, 2]
    async with factory() as db:
        chinese = await SearchGroupHistoryTool().execute(
            {"query": "华东仓 取消率"}, context, db=db,
        )
    assert chinese.success is True
    assert [hit["sequence"] for hit in chinese.data["hits"]] == [4]
    await engine.dispose()
