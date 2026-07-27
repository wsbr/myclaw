"""
Celery Beat 定时任务扫描器。
每分钟扫描 status=pending、trigger_type=scheduled、scheduled_at <= now 的 Task，
dispatch 到 execute_task。
"""
import uuid
from datetime import datetime

from croniter import croniter

from app.workers.celery_app import celery_app
from app.workers.task_worker import _run_coro, execute_task


@celery_app.task(name="workers.scheduled_worker.scan_scheduled_tasks")
def scan_scheduled_tasks():
    return _run_coro(lambda: _scan())


async def _scan():
    from app.database import AsyncSessionLocal
    from app.models.task import Task
    from sqlalchemy import select

    now = datetime.utcnow()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Task).where(
                Task.trigger_type == "scheduled",
                Task.status == "pending",
                Task.scheduled_at <= now,
            )
        )
        tasks = result.scalars().all()

        for task in tasks:
            task.status = "running"
            celery_res = execute_task.delay(task.id)
            task.celery_task_id = str(celery_res.id)

            if task.cron_expr:
                try:
                    next_dt = croniter(task.cron_expr, now).get_next(datetime)
                    db.add(Task(
                        id=str(uuid.uuid4()),
                        agent_id=task.agent_id,
                        agent_version=task.agent_version,
                        user_id=task.user_id,
                        session_id=task.session_id,
                        parent_task_id=task.parent_task_id,
                        title=task.title,
                        trigger_type="scheduled",
                        status="pending",
                        cron_expr=task.cron_expr,
                        scheduled_at=next_dt,
                        input_data=task.input_data,
                        output_channel=task.output_channel,
                    ))
                except Exception:
                    pass

        await db.commit()
