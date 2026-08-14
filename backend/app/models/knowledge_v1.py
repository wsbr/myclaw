"""Tenant-scoped local document knowledge and immutable index evidence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, utcnow, uuid7


class KnowledgeBase(Base, TimestampMixin, VersionMixin):
    __tablename__ = "knowledge_base"
    __table_args__ = (
        CheckConstraint(
            "sharing_policy IN ('owner_only','selected_agents')",
            name="ck_knowledge_base_sharing",
        ),
        CheckConstraint(
            "status IN ('active','deleted')", name="ck_knowledge_base_status",
        ),
        UniqueConstraint(
            "tenant_id", "owner_account_id", "name",
            name="uq_knowledge_base_owner_name",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_knowledge_base_id_tenant"),
        Index(
            "ix_knowledge_base_tenant_owner_status",
            "tenant_id", "owner_account_id", "status", "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False,
    )
    owner_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sharing_policy: Mapped[str] = mapped_column(
        String(24), default="owner_only", nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    current_index_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("knowledge_index_revision.id"),
    )
    next_revision_no: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    chunking_profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    embedding_profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class KnowledgeBaseAgentAccess(Base):
    __tablename__ = "knowledge_base_agent_access"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_id", "agent_id", name="uq_knowledge_base_agent_access",
        ),
        Index(
            "ix_knowledge_agent_access_tenant_agent",
            "tenant_id", "agent_id", "knowledge_base_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False,
    )
    knowledge_base_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_base.id"), nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent.id"), nullable=False,
    )
    granted_by_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False,
    )


class KnowledgeDocument(Base, TimestampMixin, VersionMixin):
    __tablename__ = "knowledge_document"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','deleted')", name="ck_knowledge_document_status",
        ),
        UniqueConstraint(
            "knowledge_base_id", "logical_key", name="uq_knowledge_document_logical_key",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_knowledge_document_id_tenant"),
        Index(
            "ix_knowledge_document_tenant_base_status",
            "tenant_id", "knowledge_base_id", "status", "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False,
    )
    knowledge_base_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_base.id"), nullable=False,
    )
    logical_key: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    current_source_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("knowledge_source.id"),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class KnowledgeSource(Base):
    __tablename__ = "knowledge_source"
    __table_args__ = (
        CheckConstraint(
            "status IN ('indexed','superseded','deleted','failed')",
            name="ck_knowledge_source_status",
        ),
        UniqueConstraint(
            "knowledge_base_id", "checksum", name="uq_knowledge_source_checksum",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_knowledge_source_id_tenant"),
        UniqueConstraint("storage_key", name="uq_knowledge_source_storage_key"),
        Index(
            "ix_knowledge_source_tenant_document_created",
            "tenant_id", "document_id", "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False,
    )
    knowledge_base_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_base.id"), nullable=False,
    )
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_document.id"), nullable=False,
    )
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    parse_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_by_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False,
    )


class KnowledgeIndexRevision(Base):
    __tablename__ = "knowledge_index_revision"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ready','failed')", name="ck_knowledge_index_status",
        ),
        UniqueConstraint(
            "knowledge_base_id", "revision_no", name="uq_knowledge_index_revision_no",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_knowledge_index_id_tenant"),
        UniqueConstraint(
            "knowledge_base_id", "content_hash",
            name="uq_knowledge_index_content_hash",
        ),
        Index(
            "ix_knowledge_index_tenant_base_revision",
            "tenant_id", "knowledge_base_id", "revision_no",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False,
    )
    knowledge_base_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_base.id"), nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_manifest: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    source_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    chunking_profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    embedding_profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    document_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_by_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False,
    )
    built_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False,
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunk"
    __table_args__ = (
        UniqueConstraint(
            "index_revision_id", "source_id", "ordinal",
            name="uq_knowledge_chunk_source_ordinal",
        ),
        Index(
            "ix_knowledge_chunk_tenant_revision",
            "tenant_id", "index_revision_id", "ordinal",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False,
    )
    index_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_index_revision.id"), nullable=False,
    )
    knowledge_base_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_base.id"), nullable=False,
    )
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_document.id"), nullable=False,
    )
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_source.id"), nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    term_frequencies: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    vector: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)


class KnowledgeRetrievalSnapshot(Base):
    __tablename__ = "knowledge_retrieval_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "runner_id", "query_hash", "revision_manifest_hash",
            name="uq_knowledge_retrieval_runner_query",
        ),
        Index(
            "ix_knowledge_retrieval_tenant_runner",
            "tenant_id", "runner_id", "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False,
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False,
    )
    runner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runner_v1.id"), nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent.id"), nullable=False,
    )
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_manifest: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    revision_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_snapshot: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False,
    )
