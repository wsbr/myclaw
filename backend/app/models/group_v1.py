"""Relationship-backed Group Formation and frozen Group runtime graph."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, uuid7


class GroupSession(Base, TimestampMixin, VersionMixin):
    __tablename__ = "group_session"
    __table_args__ = (
        CheckConstraint("status IN ('forming','open','closed','archived')", name="ck_group_session_status"),
        Index("ix_group_session_parent_status", "parent_session_id", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    parent_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    frozen_formation_revision_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    entry_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="forming", nullable=False)
    goal: Mapped[str | None] = mapped_column(Text)
    bootstrap_context: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class GroupFormationProposal(Base, TimestampMixin, VersionMixin):
    __tablename__ = "group_formation_proposal"
    __table_args__ = (
        CheckConstraint("status IN ('generating','awaiting_confirmation','confirmed','rejected','cancelled','failed')", name="ck_group_formation_status"),
        Index("ix_group_formation_parent_status", "parent_session_id", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    parent_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    group_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_session.id"), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    entry_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    generation_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="generating", nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(36))
    current_revision_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class GroupFormationRevision(Base, TimestampMixin):
    __tablename__ = "group_formation_revision"
    __table_args__ = (
        UniqueConstraint("proposal_id", "revision_no", name="uq_group_formation_revision_no"),
        UniqueConstraint("id", "proposal_id", name="uq_group_formation_revision_proposal"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    proposal_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_formation_proposal.id"), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    derived_from_revision_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("group_formation_revision.id"))
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    bootstrap_context: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    edited_by_type: Mapped[str] = mapped_column(String(20), nullable=False)
    edited_by_id: Mapped[str] = mapped_column(String(191), nullable=False)


class GroupFormationParticipant(Base):
    __tablename__ = "group_formation_participant"
    __table_args__ = (
        UniqueConstraint("revision_id", "participant_key", name="uq_group_participant_key"),
        UniqueConstraint("revision_id", "agent_id", name="uq_group_participant_agent"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    revision_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_formation_revision.id"), nullable=False)
    participant_key: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    is_entry: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    initial_responsibility: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class GroupFormationWorkItem(Base):
    __tablename__ = "group_formation_work_item"
    __table_args__ = (UniqueConstraint("revision_id", "work_item_key", name="uq_group_formation_work_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    revision_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_formation_revision.id"), nullable=False)
    work_item_key: Mapped[str] = mapped_column(String(100), nullable=False)
    participant_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_formation_participant.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class GroupFormationSuggestedEdge(Base):
    __tablename__ = "group_formation_suggested_edge"
    __table_args__ = (
        UniqueConstraint("revision_id", "source_participant_id", "target_participant_id", name="uq_group_formation_edge"),
        CheckConstraint("source_participant_id != target_participant_id", name="ck_group_formation_edge_not_self"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    revision_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_formation_revision.id"), nullable=False)
    source_participant_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_formation_participant.id"), nullable=False)
    target_participant_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_formation_participant.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class GroupAgentNode(Base, TimestampMixin, VersionMixin):
    __tablename__ = "group_agent_node"
    __table_args__ = (UniqueConstraint("group_session_id", "agent_id", name="uq_group_node_agent"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    group_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_session.id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    source_participant_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_formation_participant.id"), unique=True, nullable=False)
    is_entry: Mapped[bool] = mapped_column(Boolean, nullable=False)
    initial_responsibility: Mapped[str] = mapped_column(Text, nullable=False)
    availability: Mapped[str] = mapped_column(String(20), default="available", nullable=False)
    unavailable_reason: Mapped[str | None] = mapped_column(String(300))


class GroupInitialSuggestedEdge(Base):
    __tablename__ = "group_initial_suggested_edge"
    group_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_session.id"), primary_key=True)
    source_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_agent_node.id"), primary_key=True)
    target_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_agent_node.id"), primary_key=True)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_edge_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_formation_suggested_edge.id"), unique=True, nullable=False)


class GroupRun(Base, TimestampMixin, VersionMixin):
    __tablename__ = "group_run"
    __table_args__ = (Index("ix_group_run_session_created", "group_session_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("run.id"), unique=True, nullable=False)
    group_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_session.id"), nullable=False)
    owner_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_agent_node.id"), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)


class GroupRunAgentVersion(Base):
    __tablename__ = "group_run_agent_version"
    group_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_run.id"), primary_key=True)
    group_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_session.id"), nullable=False)
    group_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_agent_node.id"), primary_key=True)
    agent_version_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent_version.id"))
    participation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    unavailable_reason: Mapped[str | None] = mapped_column(String(300))


class GroupRunReference(Base):
    __tablename__ = "group_run_reference"
    __table_args__ = (CheckConstraint("group_run_id != referenced_group_run_id", name="ck_group_run_reference_not_self"),)
    group_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_run.id"), primary_key=True)
    referenced_group_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_run.id"), primary_key=True)


class GroupCollaborationEdge(Base, TimestampMixin):
    __tablename__ = "group_collaboration_edge"
    __table_args__ = (UniqueConstraint("group_session_id", "source_node_id", "target_node_id", name="uq_group_actual_edge"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    group_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_session.id"), nullable=False)
    source_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_agent_node.id"), nullable=False)
    target_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_agent_node.id"), nullable=False)
    first_invocation_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_invocation.id"), unique=True, nullable=False)
    created_in_group_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_run.id"), nullable=False)
