"""Message history exposes one aggregated Tool activity count per Run."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.sessions import list_messages
from app.database import Base
from app.deps import SessionActor
from app.models.common import uuid7
from app.models.foundation import PlatformAccount, UserIdentity, UserSession
from app.models.runtime_v1 import Message, ToolInvocation


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_message_history_aggregates_tool_count_without_per_message_queries():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session_id, account_id, run_id = uuid7(), uuid7(), uuid7()

    async with factory.begin() as db:
        account = PlatformAccount(
            id=account_id, user_id="history-user", display_name="History User",
            status="active",
        )
        db.add_all([
            UserIdentity(user_id="history-user", status="active"),
            account,
            UserSession(
                id=session_id, user_id="history-user", account_id=account_id,
                entry_agent_id="agent-1", status="active", origin_type="web",
            ),
            Message(
                id=uuid7(), session_id=session_id, user_id="history-user",
                sender_type="user", sender_user_id="history-user",
                role="user", content="use tools", run_id=run_id,
            ),
            Message(
                id=uuid7(), session_id=session_id, user_id="history-user",
                sender_type="agent", sender_agent_id="agent-1",
                role="assistant", content="done", run_id=run_id,
            ),
        ])
        for index in range(2):
            db.add(ToolInvocation(
                id=uuid7(), runner_id="runner-1", work_item_id="work-1",
                run_id=run_id, session_id=session_id,
                platform_tool_id="tool-1", tool_key=f"lookup_{index}",
                tool_version_id="tool-version-1",
                tool_version_content_hash="a" * 64,
                execution_mode="inline", status="succeeded",
                arguments={}, arguments_hash="b" * 64,
                business_idempotency_key=f"operation-{index}",
                principal_hash="c" * 64,
            ))

    async with factory() as db:
        account = await db.get(PlatformAccount, account_id)
        result = await list_messages(
            session_id=session_id, scope="chat", limit=100, cursor=None,
            db=db, actor=SessionActor(account=account),
        )

    assistant = next(item for item in result["items"] if item["role"] == "assistant")
    assert assistant["tool_activity_count"] == 2
    await engine.dispose()
