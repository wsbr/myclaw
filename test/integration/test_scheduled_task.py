"""Integration tests for create_scheduled_task tool and scheduled worker scan."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tool_registry.base import RuntimeContext
from app.core.tool_registry.builtin.create_scheduled_task import CreateScheduledTaskTool
from app.models.agent import Agent
from app.models.session import Session
from app.models.task import Task
from app.workers.scheduled_worker import _scan

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _seed_agent_and_session(db: AsyncSession) -> tuple[Agent, Session, str]:
    """Return (agent, session, user_id) using admin user from clean_db."""
    from app.models.user import User

    user_res = await db.execute(
        select(User).where(User.email == os.environ["INIT_ADMIN_EMAIL"])
    )
    user = user_res.scalar_one()

    agent = Agent(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        name=f"sched-agent-{uuid.uuid4().hex[:6]}",
        description="Scheduled task test agent",
        modes=["chat"],
        version=1,
        status="active",
        model_config_={"provider_id": "p1", "model_id": "mock"},
        system_prompt_blocks={"blocks": []},
    )
    db.add(agent)
    await db.flush()

    session = Session(
        id=str(uuid.uuid4()),
        agent_id=agent.id,
        user_id=user.id,
        mode="chat",
    )
    db.add(session)
    await db.commit()
    await db.refresh(agent)
    await db.refresh(session)
    return agent, session, user.id


@pytest.mark.asyncio
async def test_create_scheduled_task_delay_persists(clean_db, db_engine, mock_model_hub):
    import app.database as db_module

    db_module.engine = db_engine
    db_module.AsyncSessionLocal = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)

    async with db_module.AsyncSessionLocal() as db:
        agent, session, user_id = await _seed_agent_and_session(db)
        tool = CreateScheduledTaskTool()
        context = RuntimeContext(
            session_id=session.id,
            runner_id=str(uuid.uuid4()),
            user_id=user_id,
            agent_id=agent.id,
        )

        result = await tool.execute(
            {"title": "复习提醒", "description": "提醒用户复习 BFS", "delay": "10m"},
            context,
            db=db,
        )

        assert result.success is True
        task_id = result.data["task_id"]

        row = await db.get(Task, task_id)
        assert row is not None
        assert row.trigger_type == "scheduled"
        assert row.status == "pending"
        assert row.cron_expr is None
        assert row.input_data == {"description": "提醒用户复习 BFS"}
        assert row.scheduled_at > datetime.utcnow()


@pytest.mark.asyncio
async def test_create_scheduled_task_cron_persists(clean_db, db_engine, mock_model_hub):
    import app.database as db_module

    db_module.engine = db_engine
    db_module.AsyncSessionLocal = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)

    async with db_module.AsyncSessionLocal() as db:
        agent, session, user_id = await _seed_agent_and_session(db)
        tool = CreateScheduledTaskTool()
        context = RuntimeContext(
            session_id=session.id,
            runner_id=str(uuid.uuid4()),
            user_id=user_id,
            agent_id=agent.id,
        )

        result = await tool.execute(
            {"title": "日报", "description": "生成日报", "cron_expr": "0 8 * * *"},
            context,
            db=db,
        )

        assert result.success is True
        row = await db.get(Task, result.data["task_id"])
        assert row.cron_expr == "0 8 * * *"
        assert row.scheduled_at > datetime.utcnow()


@pytest.mark.asyncio
async def test_create_scheduled_task_at_iso8601(clean_db, db_engine, mock_model_hub):
    import app.database as db_module

    db_module.engine = db_engine
    db_module.AsyncSessionLocal = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)

    async with db_module.AsyncSessionLocal() as db:
        agent, session, user_id = await _seed_agent_and_session(db)
        tool = CreateScheduledTaskTool()
        context = RuntimeContext(
            session_id=session.id,
            runner_id=str(uuid.uuid4()),
            user_id=user_id,
            agent_id=agent.id,
        )

        result = await tool.execute(
            {
                "title": "定点提醒",
                "description": "20:00 提醒",
                "at": "2026-07-10T20:00:00",
            },
            context,
            db=db,
        )

        assert result.success is True
        row = await db.get(Task, result.data["task_id"])
        assert row.scheduled_at == datetime(2026, 7, 10, 20, 0, 0)


@pytest.mark.asyncio
async def test_scan_dispatches_due_task_and_schedules_cron_next(
    clean_db, db_engine, mock_model_hub, celery_eager
):
    import app.database as db_module

    db_module.engine = db_engine
    db_module.AsyncSessionLocal = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)

    async with db_module.AsyncSessionLocal() as db:
        agent, _session, user_id = await _seed_agent_and_session(db)
        due_task = Task(
            id=str(uuid.uuid4()),
            agent_id=agent.id,
            agent_version=agent.version,
            user_id=user_id,
            title="Cron 日报",
            trigger_type="scheduled",
            status="pending",
            cron_expr="0 8 * * *",
            scheduled_at=datetime.utcnow() - timedelta(minutes=1),
            input_data={"description": "生成日报"},
            output_channel="platform_ui",
        )
        db.add(due_task)
        await db.commit()

    with patch("app.runtime.agent_runtime.ModelHub.get", return_value=mock_model_hub):
        await _scan()

    async with db_module.AsyncSessionLocal() as db:
        original = await db.get(Task, due_task.id)
        assert original.status == "running"
        assert original.celery_task_id

        pending = (
            await db.execute(
                select(Task).where(
                    Task.trigger_type == "scheduled",
                    Task.status == "pending",
                    Task.title == "Cron 日报",
                    Task.id != due_task.id,
                )
            )
        ).scalars().all()
        assert len(pending) == 1
        assert pending[0].cron_expr == "0 8 * * *"
        assert pending[0].scheduled_at > datetime.utcnow()


@pytest.mark.asyncio
async def test_scan_dispatches_one_shot_delay_without_recurrence(
    clean_db, db_engine, mock_model_hub, celery_eager
):
    import app.database as db_module

    db_module.engine = db_engine
    db_module.AsyncSessionLocal = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)

    async with db_module.AsyncSessionLocal() as db:
        agent, _session, user_id = await _seed_agent_and_session(db)
        due_task = Task(
            id=str(uuid.uuid4()),
            agent_id=agent.id,
            agent_version=agent.version,
            user_id=user_id,
            title="5分钟后提醒",
            trigger_type="scheduled",
            status="pending",
            cron_expr=None,
            scheduled_at=datetime.utcnow() - timedelta(seconds=30),
            input_data={"description": "提醒用户复习"},
            output_channel="platform_ui",
        )
        db.add(due_task)
        await db.commit()

    with patch("app.runtime.agent_runtime.ModelHub.get", return_value=mock_model_hub):
        await _scan()

    async with db_module.AsyncSessionLocal() as db:
        original = await db.get(Task, due_task.id)
        assert original.status == "running"

        others = (
            await db.execute(
                select(Task).where(Task.title == "5分钟后提醒", Task.id != due_task.id)
            )
        ).scalars().all()
        assert others == []
