"""Unit tests for create_scheduled_task builtin tool."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.core.tool_registry.builtin.create_scheduled_task import CreateScheduledTaskTool
from app.core.tool_registry.base import RuntimeContext

pytestmark = pytest.mark.unit


def _mock_db(agent_version: int = 1):
    db = AsyncMock()
    agent = MagicMock()
    agent.version = agent_version
    result = MagicMock()
    result.scalar_one_or_none.return_value = agent
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_create_with_delay():
    tool = CreateScheduledTaskTool()
    context = RuntimeContext(
        session_id="sess-1",
        runner_id="run-1",
        user_id="user-1",
        agent_id="agent-1",
    )

    result = await tool.execute(
        {"title": "复习提醒", "description": "提醒用户复习 BFS", "delay": "5m"},
        context,
        db=_mock_db(),
    )

    assert result.success is True
    assert result.data["task_id"]
    assert result.data["scheduled_at"]
    assert result.data["delay"] == "5m"
    assert "cron_expr" not in result.data


@pytest.mark.asyncio
async def test_create_with_cron():
    tool = CreateScheduledTaskTool()
    context = RuntimeContext(
        session_id="sess-1",
        runner_id="run-1",
        user_id="user-1",
        agent_id="agent-1",
    )

    result = await tool.execute(
        {"title": "日报", "description": "生成日报", "cron_expr": "0 8 * * *"},
        context,
        db=_mock_db(),
    )

    assert result.success is True
    assert result.data["cron_expr"] == "0 8 * * *"


@pytest.mark.asyncio
async def test_requires_schedule():
    tool = CreateScheduledTaskTool()
    context = RuntimeContext(
        session_id="sess-1",
        runner_id="run-1",
        user_id="user-1",
        agent_id="agent-1",
    )

    result = await tool.execute(
        {"title": "t", "description": "d"},
        context,
        db=_mock_db(),
    )
    assert result.success is False
