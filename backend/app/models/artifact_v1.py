"""Lightweight immutable Artifact registrations backed by Session Workspace files."""

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, uuid7


class Artifact(Base, TimestampMixin, VersionMixin):
    __tablename__ = "artifact"
    __table_args__ = (
        CheckConstraint("status IN ('publishing','published','unavailable','deleted')", name="ck_artifact_status"),
        UniqueConstraint("session_id", "relative_path", "version_no", name="uq_artifact_path_version"),
        UniqueConstraint("parent_artifact_id", "version_no", name="uq_artifact_parent_version"),
        UniqueConstraint("tool_invocation_id", name="uq_artifact_tool_invocation"),
        Index("ix_artifact_session_status_created", "session_id", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    user_id: Mapped[str] = mapped_column(String(191), ForeignKey("user_identity.user_id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_session.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspace.id"), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    source_relative_path: Mapped[str | None] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent.id"), nullable=False)
    work_item_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("work_item.id"))
    runner_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("runner_v1.id"))
    tool_invocation_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tool_invocation.id"))
    parent_artifact_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("artifact.id"))
    status: Mapped[str] = mapped_column(String(20), default="published", nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)


class MessageArtifact(Base):
    __tablename__ = "message_artifact"
    __table_args__ = (
        CheckConstraint("visibility IN ('user','group','direct')", name="ck_message_artifact_visibility"),
    )
    message_id: Mapped[str] = mapped_column(String(36), ForeignKey("message.id"), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(36), ForeignKey("artifact.id"), primary_key=True)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)


class GroupArtifact(Base):
    __tablename__ = "group_artifact"
    __table_args__ = (
        CheckConstraint(
            "(published_by_node_id IS NOT NULL) <> (published_by_membership_id IS NOT NULL)",
            name="ck_group_artifact_publisher_scope",
        ),
    )
    group_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_session.id"), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(36), ForeignKey("artifact.id"), primary_key=True)
    published_by_node_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("group_agent_node.id")
    )
    published_by_membership_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("group_membership.id")
    )
    message_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("message.id"))
