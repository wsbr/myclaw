import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Integer, Text, JSON, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id"), nullable=False)
    agent_version: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sessions.id"), nullable=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        SAEnum("pending", "running", "waiting", "completed", "failed", "cancelled"),
        nullable=False,
        default="pending"
    )
    waiting_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    trigger_type: Mapped[str] = mapped_column(
        SAEnum("immediate", "scheduled", "event"),
        nullable=False,
        default="immediate"
    )
    cron_expr: Mapped[str | None] = mapped_column(String(100), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    input_data: Mapped[dict | None] = mapped_column("input", JSON, nullable=True)
    output_data: Mapped[dict | None] = mapped_column("output", JSON, nullable=True)
    output_channel: Mapped[str] = mapped_column(String(50), nullable=False, default="platform_ui")
    celery_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
