"""Phase-one model, tool, skill, policy, and sandbox registries."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class RegisteredModel(Base, TimestampMixin):
    __tablename__ = "model"
    __table_args__ = (Index("ix_model_provider_enabled", "provider", "enabled"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    key: Mapped[str] = mapped_column(String(191), unique=True, nullable=False)
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


class PlatformTool(Base, TimestampMixin):
    __tablename__ = "platform_tool"
    __table_args__ = (
        CheckConstraint(
            "execution_mode IN ('inline','durable_async')", name="ck_platform_tool_mode"
        ),
        CheckConstraint(
            "confirmation_policy IN ('never','always')",
            name="ck_platform_tool_confirmation",
        ),
        Index("ix_platform_tool_enabled_key", "enabled", "tool_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tool_key: Mapped[str] = mapped_column(String(191), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    output_schema: Mapped[dict | None] = mapped_column(JSON)
    implementation_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    implementation_version: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), default="inline", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    confirmation_policy: Mapped[str] = mapped_column(String(20), default="never", nullable=False)
    platform_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    config_version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    registration_hash: Mapped[str] = mapped_column(String(64), nullable=False)


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
            "status IN ('uploaded','validating','published','rejected','deprecated')",
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


class PolicyDefinition(Base, TimestampMixin):
    __tablename__ = "policy_definition"
    __table_args__ = (
        CheckConstraint(
            "policy_type IN ('context','history','memory_write','memory_recall')",
            name="ck_policy_definition_type",
        ),
        Index("ix_policy_definition_type_enabled", "policy_type", "enabled"),
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


class SandboxProfile(Base, TimestampMixin):
    __tablename__ = "sandbox_profile"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    profile_key: Mapped[str] = mapped_column(String(191), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    implementation_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    config_version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
