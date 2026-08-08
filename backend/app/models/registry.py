"""Phase-one model, tool, skill, policy, and sandbox registries."""

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
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, utcnow, uuid7


class RegisteredModel(Base, TimestampMixin, VersionMixin):
    __tablename__ = "model"
    __table_args__ = (
        CheckConstraint("status IN ('active','inactive')", name="ck_model_status"),
        Index("ix_model_provider_enabled", "provider", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    key: Mapped[str] = mapped_column(String(191), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(191), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("model_version.id", use_alter=True, name="fk_model_current_version")
    )
    next_version_no: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    # The fields below remain readable while the expand/contract migration
    # moves existing UAT registry rows into immutable ModelVersion records.
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(500))
    credential_ref: Mapped[str | None] = mapped_column(String(500))
    default_parameters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    parameter_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config_version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    created_by_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform_account.id")
    )


class ModelVersion(Base):
    """Immutable provider behavior snapshot selected by an AgentVersion."""

    __tablename__ = "model_version"
    __table_args__ = (
        UniqueConstraint("model_id", "version_no", name="uq_model_version_number"),
        UniqueConstraint("model_id", "content_hash", name="uq_model_version_hash"),
        Index("ix_model_version_model_published", "model_id", "published_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    model_id: Mapped[str] = mapped_column(String(36), ForeignKey("model.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(String(191), nullable=False)
    model_name: Mapped[str] = mapped_column(String(191), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(500))
    credential_ref: Mapped[str | None] = mapped_column(String(500))
    default_parameters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    adapter_protocol: Mapped[str] = mapped_column(
        String(64), default="agent_native.v2", nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )
    published_by_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform_account.id")
    )


class ConformanceRun(Base, TimestampMixin, VersionMixin):
    """Append-only evidence; a rerun creates a new row instead of overwriting."""

    __tablename__ = "conformance_run"
    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('model_version','platform_tool','channel_plugin','sandbox_profile_version')",
            name="ck_conformance_subject_type",
        ),
        CheckConstraint(
            "status IN ('queued','running','passed','failed')",
            name="ck_conformance_status",
        ),
        UniqueConstraint(
            "subject_type", "subject_id", "suite_version", "idempotency_key",
            name="uq_conformance_run_request",
        ),
        Index(
            "ix_conformance_subject_terminal",
            "subject_type", "subject_id", "suite_version", "status", "ended_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False)
    suite_version: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    result_summary: Mapped[dict | None] = mapped_column(JSON)
    evidence_artifact_id: Mapped[str | None] = mapped_column(String(36))
    content_hash: Mapped[str | None] = mapped_column(String(64))


class PlatformSecret(Base, TimestampMixin, VersionMixin):
    """Write-only, encrypted platform credential material.

    The ciphertext is never projected by an API. ``fingerprint`` is the only
    stable value copied into immutable registry and execution snapshots.
    """

    __tablename__ = "platform_secret"
    __table_args__ = (
        CheckConstraint("status IN ('active','rotated','revoked')", name="ck_platform_secret_status"),
        Index("ix_platform_secret_purpose_status", "purpose", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    purpose: Mapped[str] = mapped_column(String(100), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    rotated_from_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform_secret.id")
    )
    created_by_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform_account.id")
    )


class ToolDefinition(Base, TimestampMixin, VersionMixin):
    """Stable, provider-neutral logical Tool identity."""

    # Keep the physical name stable while replacing the old mutable contract.
    # The breaking reset means no legacy rows survive this semantic change.
    __tablename__ = "platform_tool"
    __table_args__ = (
        CheckConstraint("origin IN ('builtin','runtime','mcp')", name="ck_tool_definition_origin"),
        CheckConstraint("status IN ('active','inactive')", name="ck_tool_definition_status"),
        UniqueConstraint("namespace", "tool_key", name="uq_tool_definition_namespace_key"),
        UniqueConstraint("tool_key", name="uq_tool_definition_key"),
        Index("ix_tool_definition_status_key", "status", "tool_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tool_key: Mapped[str] = mapped_column(String(191), nullable=False)
    namespace: Mapped[str] = mapped_column(String(100), default="platform", nullable=False)
    origin: Mapped[str] = mapped_column(String(20), default="builtin", nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name_zh: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey(
            "tool_version.id", use_alter=True, name="fk_tool_definition_current_version"
        )
    )
    next_version_no: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)


class ToolConnection(Base, TimestampMixin, VersionMixin):
    """Stable administrator-owned connection identity and health projection."""

    __tablename__ = "tool_connection"
    __table_args__ = (
        CheckConstraint(
            "connection_type IN ('bocha','brave','mcp','browser')",
            name="ck_tool_connection_type",
        ),
        CheckConstraint(
            "auth_type IN ('none','bearer','x_api_key')",
            name="ck_tool_connection_auth",
        ),
        CheckConstraint(
            "status IN ('active','inactive')", name="ck_tool_connection_status"
        ),
        UniqueConstraint("connection_key", name="uq_tool_connection_key"),
        Index("ix_tool_connection_type_primary", "connection_type", "is_primary"),
        Index("uq_tool_connection_single_primary", "primary_search_slot", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    connection_key: Mapped[str] = mapped_column(String(191), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    connection_type: Mapped[str] = mapped_column(String(32), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="inactive", nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    primary_search_slot: Mapped[int | None] = mapped_column(
        Integer,
        Computed("CASE WHEN is_primary = 1 THEN 1 ELSE NULL END", persisted=True),
    )
    current_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey(
            "tool_connection_revision.id", use_alter=True,
            name="fk_tool_connection_current_revision",
        )
    )
    next_revision_no: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    health_status: Mapped[str] = mapped_column(String(32), default="untested", nullable=False)
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    health_latency_ms: Mapped[int | None] = mapped_column(Integer)
    health_error_code: Mapped[str | None] = mapped_column(String(100))
    created_by_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform_account.id")
    )


class ToolConnectionRevision(Base):
    """Immutable endpoint/config/secret snapshot used by a Tool execution."""

    __tablename__ = "tool_connection_revision"
    __table_args__ = (
        UniqueConstraint("connection_id", "revision_no", name="uq_tool_connection_revision_no"),
        UniqueConstraint("connection_id", "content_hash", name="uq_tool_connection_revision_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tool_connection.id"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    endpoint: Mapped[str] = mapped_column(String(1000), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(20), nullable=False)
    secret_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform_secret.id")
    )
    secret_fingerprint: Mapped[str | None] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )
    created_by_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform_account.id")
    )


class ToolVersion(Base):
    """Immutable model-facing contract and implementation identity."""

    __tablename__ = "tool_version"
    __table_args__ = (
        CheckConstraint(
            "execution_mode IN ('inline','durable_async')", name="ck_tool_version_mode"
        ),
        CheckConstraint(
            "side_effect IN ('none','idempotent','non_idempotent')",
            name="ck_tool_version_side_effect",
        ),
        UniqueConstraint("definition_id", "version_no", name="uq_tool_version_number"),
        UniqueConstraint("definition_id", "content_hash", name="uq_tool_version_hash"),
        Index("ix_tool_version_definition_published", "definition_id", "published_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_tool.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_description: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    output_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    examples: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    error_catalog: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    annotations: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    implementation_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    implementation_version: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    side_effect: Mapped[str] = mapped_column(String(20), nullable=False)
    supports_cancel: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_status_lookup: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_connection.id")
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )


class ToolGovernance(Base, TimestampMixin, VersionMixin):
    """Mutable policy kept separate from immutable Tool contracts."""

    __tablename__ = "tool_governance"
    __table_args__ = (
        CheckConstraint(
            "confirmation_policy IN ('never','per_call','session_grant')",
            name="ck_tool_governance_confirmation",
        ),
    )

    tool_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_tool.id"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    confirmation_policy: Mapped[str] = mapped_column(String(20), default="never", nullable=False)
    network_policy: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    workspace_policy: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    risk_rules: Mapped[list] = mapped_column(JSON, default=list, nullable=False)


class RegisteredSkill(Base, TimestampMixin, VersionMixin):
    __tablename__ = "skill"
    __table_args__ = (
        CheckConstraint("status IN ('active','inactive')", name="ck_skill_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    logical_name: Mapped[str] = mapped_column(String(191), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    owner_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    current_published_version_id: Mapped[str | None] = mapped_column(String(36))


class SkillVersion(Base):
    __tablename__ = "skill_version"
    __table_args__ = (
        UniqueConstraint("skill_id", "version_no", name="uq_skill_version_number"),
        UniqueConstraint("skill_id", "checksum", name="uq_skill_version_checksum"),
        CheckConstraint(
            "status IN ('uploaded','validating','published','rejected','deprecated','quarantined')",
            name="ck_skill_version_status",
        ),
        Index("ix_skill_version_status", "skill_id", "status", "version_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    skill_id: Mapped[str] = mapped_column(String(36), ForeignKey("skill.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="uploaded", nullable=False)
    package_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    manifest_name: Mapped[str] = mapped_column(String(200), nullable=False)
    manifest_description: Mapped[str] = mapped_column(Text, nullable=False)
    validation_error: Mapped[str | None] = mapped_column(Text)
    created_by_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )


class PolicyDefinition(Base, TimestampMixin, VersionMixin):
    __tablename__ = "policy_definition"
    __table_args__ = (
        CheckConstraint(
            "policy_type IN ('context','history','memory_write','memory_recall')",
            name="ck_policy_definition_type",
        ),
        Index("ix_policy_definition_type_enabled", "policy_type", "enabled"),
        CheckConstraint("status IN ('active','inactive')", name="ck_policy_definition_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    policy_key: Mapped[str] = mapped_column(String(191), unique=True, nullable=False)
    policy_type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    implementation_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    implementation_version: Mapped[str] = mapped_column(String(64), nullable=False)
    default_parameters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    parameter_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_version.id", use_alter=True, name="fk_policy_current_version")
    )
    next_version_no: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)


class PolicyVersion(Base):
    __tablename__ = "policy_version"
    __table_args__ = (
        CheckConstraint(
            "status IN ('published','deprecated')", name="ck_policy_version_status"
        ),
        UniqueConstraint(
            "policy_definition_id", "version_no", name="uq_policy_version_number"
        ),
        UniqueConstraint(
            "policy_definition_id", "content_hash", name="uq_policy_version_hash"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    policy_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("policy_definition.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    implementation_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="published", nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )
    version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)


class SandboxProfile(Base, TimestampMixin, VersionMixin):
    __tablename__ = "sandbox_profile"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    profile_key: Mapped[str] = mapped_column(String(191), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    implementation_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    config_version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey(
            "sandbox_profile_version.id", use_alter=True,
            name="fk_sandbox_current_version",
        )
    )
    next_version_no: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)


class SandboxProfileVersion(Base):
    __tablename__ = "sandbox_profile_version"
    __table_args__ = (
        CheckConstraint(
            "status IN ('published','deprecated')", name="ck_sandbox_version_status"
        ),
        CheckConstraint(
            "security_level IN ('dev_directory','microvm')",
            name="ck_sandbox_version_security",
        ),
        UniqueConstraint(
            "sandbox_profile_id", "version_no", name="uq_sandbox_version_number"
        ),
        UniqueConstraint(
            "sandbox_profile_id", "content_hash", name="uq_sandbox_version_hash"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    sandbox_profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sandbox_profile.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    runtime_class: Mapped[str] = mapped_column(String(191), nullable=False)
    image_digest: Mapped[str | None] = mapped_column(String(191))
    workspace_protocol_version: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_protocol_version: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_policy: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    egress_policy: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    mount_policy: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    security_level: Mapped[str] = mapped_column(
        String(32), default="dev_directory", nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="published", nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )
    version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
