from celery import Celery
from app.config import settings

from celery.schedules import crontab

celery_app = Celery(
    "myclaw",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.task_worker", "app.workers.scheduled_worker"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    beat_schedule={
        "scan-scheduled-tasks": {
            "task": "workers.scheduled_worker.scan_scheduled_tasks",
            "schedule": crontab(minute="*"),  # 每分钟扫描一次
        },
    },
)
