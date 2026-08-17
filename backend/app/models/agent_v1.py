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
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
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
        UniqueConstraint("id", "tenant_id", name="uq_agent_id_tenant"),
        Index("ix_agent_tenant_visibility_status", "tenant_id", "visibility", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False
    )
    owner_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    visibility: Mapped[str] = mapped_column(String(20), default="private", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    draft_version_id: Mapped[str | None] = mapped_column(String(36))
    current_published_version_id: Mapped[str | None] = mapped_column(String(36))
    next_version_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class AgentMember(Base, TimestampMixin, VersionMixin):
    """A revocable use grant for one private Agent.

    Ownership remains on ``AgentBlueprint``.  This record grants discovery and
    execution only; it is never consulted by Agent management endpoints.
    """

    __tablename__ = "agent_member"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','revoked')", name="ck_agent_member_status"
        ),
        UniqueConstraint("agent_id", "account_id", name="uq_agent_member_account"),
        ForeignKeyConstraint(
            ["agent_id", "tenant_id"], ["agent.id", "agent.tenant_id"],
            name="fk_agent_member_agent_tenant",
        ),
        ForeignKeyConstraint(
            ["account_id", "tenant_id"],
            ["platform_account.id", "platform_account.tenant_id"],
            name="fk_agent_member_account_tenant",
        ),
        ForeignKeyConstraint(
            ["granted_by_account_id", "tenant_id"],
            ["platform_account.id", "platform_account.tenant_id"],
            name="fk_agent_member_grantor_tenant",
        ),
        Index("ix_agent_member_account_status", "account_id", "status", "agent_id"),
        Index("ix_agent_member_agent_status", "agent_id", "status", "account_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    granted_by_account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class AgentAssemblyVersion(Base, TimestampMixin, VersionMixin):
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
    model_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("model_version.id")
    )
    sandbox_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sandbox_profile.id")
    )
    sandbox_profile_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sandbox_profile_version.id")
    )
    context_policy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_definition.id")
    )
    context_policy_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_version.id")
    )
    history_policy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_definition.id")
    )
    history_policy_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_version.id")
    )
    memory_write_policy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_definition.id")
    )
    memory_write_policy_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_version.id")
    )
    memory_recall_policy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_definition.id")
    )
    memory_recall_policy_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_version.id")
    )
    group_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    discovery_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    collaboration_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
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
    tool_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_version.id")
    )
    tool_version_content_hash: Mapped[str | None] = mapped_column(String(64))
    connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_connection.id")
    )
    connection_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_connection_revision.id")
    )
    secret_fingerprint: Mapped[str | None] = mapped_column(String(64))
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
    skill_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("skill_version.id")
    )
    checksum: Mapped[str | None] = mapped_column(String(64))
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


class AgentCard(Base):
    """Safe, immutable network projection derived at publish time."""

    __tablename__ = "agent_card"
    __table_args__ = (
        UniqueConstraint("agent_version_id", name="uq_agent_card_version"),
        Index(
            "ix_agent_card_discovery", "visibility", "availability", "group_enabled"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    agent_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_version.id"), nullable=False
    )
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(191), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    identity_kind: Mapped[str] = mapped_column(String(64), default="agent", nullable=False)
    domain_tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    supported_outcomes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    accepted_request_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    capabilities_summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    collaboration_summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    declaration: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    attestation: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    availability: Mapped[str] = mapped_column(String(20), default="available", nullable=False)
    group_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
