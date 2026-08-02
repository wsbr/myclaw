"""Stable Agent identity and immutable phase-one assembly versions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, uuid7


class AgentBlueprint(Base, TimestampMixin, VersionMixin):
    __tablename__ = "agent"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('private','enterprise')", name="ck_agent_visibility"
        ),
        CheckConstraint(
            "status IN ('draft','published','inactive')", name="ck_agent_status"
        ),
        Index("ix_agent_owner_status", "owner_account_id", "status"),
        Index("ix_agent_visibility_status", "visibility", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    owner_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    visibility: Mapped[str] = mapped_column(String(20), default="private", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    draft_version_id: Mapped[str | None] = mapped_column(String(36))
    current_published_version_id: Mapped[str | None] = mapped_column(String(36))
    next_version_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class AgentAssemblyVersion(Base, TimestampMixin):
    __tablename__ = "agent_version"
    __table_args__ = (
        UniqueConstraint("agent_id", "version_no", name="uq_agent_version_number"),
        UniqueConstraint("draft_agent_id", name="uq_agent_single_draft"),
        CheckConstraint(
            "publication_status IN ('draft','published','deprecated','draft_test_snapshot')",
            name="ck_agent_version_publication_status",
        ),
        CheckConstraint(
            "(publication_status IN ('published','deprecated') AND version_no IS NOT NULL) OR "
            "(publication_status IN ('draft','draft_test_snapshot') AND version_no IS NULL)",
            name="ck_agent_version_number_status",
        ),
        Index("ix_agent_version_agent_status", "agent_id", "publication_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent.id"), nullable=False
    )
    version_no: Mapped[int | None] = mapped_column(Integer)
    publication_status: Mapped[str] = mapped_column(String(32), nullable=False)
    draft_revision: Mapped[int | None] = mapped_column(BigInteger)
    draft_agent_id: Mapped[str | None] = mapped_column(
        String(36),
        Computed(
            "CASE WHEN publication_status = 'draft' THEN agent_id ELSE NULL END",
            persisted=True,
        ),
    )
    derived_from_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_version.id")
    )
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    instructions: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("model.id"))
    sandbox_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sandbox_profile.id")
    )
    context_policy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_definition.id")
    )
    history_policy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_definition.id")
    )
    memory_write_policy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_definition.id")
    )
    memory_recall_policy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_definition.id")
    )
    group_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    immutable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class AgentVersionTool(Base):
    __tablename__ = "agent_version_tool"
    __table_args__ = (
        UniqueConstraint("agent_version_id", "sort_order", name="uq_agent_version_tool_order"),
    )

    agent_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_version.id", ondelete="CASCADE"), primary_key=True
    )
    platform_tool_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_tool.id"), primary_key=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentVersionSkill(Base):
    __tablename__ = "agent_version_skill"
    __table_args__ = (
        UniqueConstraint("agent_version_id", "sort_order", name="uq_agent_version_skill_order"),
    )

    agent_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_version.id", ondelete="CASCADE"), primary_key=True
    )
    skill_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("skill.id"), primary_key=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentVersionBinding(Base):
    __tablename__ = "agent_version_binding"
    __table_args__ = (
        UniqueConstraint("agent_version_id", "sort_order", name="uq_agent_version_binding_order"),
    )

    agent_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_version.id", ondelete="CASCADE"), primary_key=True
    )
    bound_agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent.id"), primary_key=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
