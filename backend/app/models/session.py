import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Integer, Text, JSON, ForeignKey, Enum as SAEnum, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    mode: Mapped[str] = mapped_column(SAEnum("chat", "plan", "group"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(SAEnum("active", "completed", "archived"), default="active")
    fork_from_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    fork_from_runner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    fork_from_span_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    runners: Mapped[list["Runner"]] = relationship("Runner", back_populates="session", lazy="select")
    orchestration_graph: Mapped["OrchestrationGraph | None"] = relationship("OrchestrationGraph", back_populates="session", uselist=False, lazy="select")


class Runner(Base):
    __tablename__ = "runners"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id"), nullable=False)
    agent_version: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    parent_runner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(
        SAEnum("pending", "running", "completed", "failed", "cancelled"),
        default="pending"
    )
    input_data: Mapped[dict | None] = mapped_column("input", JSON, nullable=True)
    output_data: Mapped[dict | None] = mapped_column("output", JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(default=0.0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["Session"] = relationship("Session", back_populates="runners")
    spans: Mapped[list["Span"]] = relationship("Span", back_populates="runner", lazy="select")


class Span(Base):
    __tablename__ = "spans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    runner_id: Mapped[str] = mapped_column(String(36), ForeignKey("runners.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    span_type: Mapped[str] = mapped_column(
        SAEnum("llm_call", "tool_call", "agent_call", "memory", "event", "plan_step"),
        nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(SAEnum("running", "completed", "failed"), default="running")
    snapshot: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    request: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    response: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    runner: Mapped["Runner"] = relationship("Runner", back_populates="spans")


class OrchestrationGraph(Base):
    __tablename__ = "orchestration_graphs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), unique=True, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    graph_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        SAEnum("draft", "confirmed", "running", "completed", "failed"),
        default="draft"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    session: Mapped["Session"] = relationship("Session", back_populates="orchestration_graph")
