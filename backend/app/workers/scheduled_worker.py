"""
Celery Beat 定时任务扫描器。
每分钟扫描一次 status=pending、trigger_type=scheduled、next_run_at <= now 的 Task，
dispatch 到 execute_task。
"""
import asyncio
from datetime import datetime, timezone
from app.workers.celery_app import celery_app


@celery_app.task(name="workers.scheduled_worker.scan_scheduled_tasks")
def scan_scheduled_tasks():
    asyncio.get_event_loop().run_until_complete(_scan())


async def _scan():
    from app.database import AsyncSessionLocal
    from app.models.task import Task
    from app.workers.task_worker import execute_task
    from sqlalchemy import select
    from croniter import croniter

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Task).where(
                Task.trigger_type == "scheduled",
                Task.status == "pending",
                Task.next_run_at <= now,
            )
        )
        tasks = result.scalars().all()

        for task in tasks:
            task.status = "running"
            celery_id = execute_task.delay(task.id)
            task.celery_task_id = str(celery_id)

            # 如果有 cron 表达式，更新下次运行时间
            if task.cron_expr:
                try:
                    cron = croniter(task.cron_expr, now)
                    next_dt = cron.get_next(datetime)
                    # 创建同配置的新 pending Task 代表下次执行
                    import uuid
                    new_task = Task(
                        id=str(uuid.uuid4()),
                        agent_id=task.agent_id,
                        agent_version=task.agent_version,
                        user_id=task.user_id,
                        title=task.title,
                        trigger_type="scheduled",
                        status="pending",
                        cron_expr=task.cron_expr,
                        next_run_at=next_dt,
                        input_data=task.input_data,
                        output_channel=task.output_channel,
                    )
                    db.add(new_task)
                except Exception:
                    pass

        await db.commit()
