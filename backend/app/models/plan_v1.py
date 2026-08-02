"""Relationship-backed Plan definition and frozen participant set."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, uuid7


class PlanExecution(Base, TimestampMixin, VersionMixin):
    __tablename__ = "plan_execution"
    __table_args__ = (
        CheckConstraint(
            "definition_status IN ('draft','awaiting_confirmation','frozen','cancelled')",
            name="ck_plan_definition_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), unique=True, nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    owner_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    owner_agent_version_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent_version.id"))
    predecessor_plan_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("plan_execution.id"))
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    deliverable: Mapped[str] = mapped_column(Text, default="", nullable=False)
    definition_status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    graph_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    frozen_hash: Mapped[str | None] = mapped_column(String(64))


class PlanWorkItemNode(Base):
    __tablename__ = "plan_work_item_node"
    __table_args__ = (
        UniqueConstraint("plan_execution_id", "node_key", name="uq_plan_node_key"),
        UniqueConstraint("plan_execution_id", "sort_order", name="uq_plan_node_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    plan_execution_id: Mapped[str] = mapped_column(String(36), ForeignKey("plan_execution.id"), nullable=False)
    node_key: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    agent_version_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent_version.id"))
    work_item_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("work_item.id"), unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_version: Mapped[int] = mapped_column(Integer, nullable=False)


class PlanWorkItemDependency(Base):
    __tablename__ = "plan_work_item_dependency"
    __table_args__ = (
        CheckConstraint("predecessor_node_id != successor_node_id", name="ck_plan_dependency_not_self"),
        CheckConstraint("dependency_type IN ('success','settled')", name="ck_plan_dependency_type"),
    )

    plan_execution_id: Mapped[str] = mapped_column(String(36), ForeignKey("plan_execution.id"), primary_key=True)
    predecessor_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("plan_work_item_node.id"), primary_key=True)
    successor_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("plan_work_item_node.id"), primary_key=True)
    dependency_type: Mapped[str] = mapped_column(String(20), default="success", nullable=False)


class PlanDeclaredAgent(Base):
    __tablename__ = "plan_declared_agent"
    plan_execution_id: Mapped[str] = mapped_column(String(36), ForeignKey("plan_execution.id"), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), primary_key=True)
    agent_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_version.id"), nullable=False)
