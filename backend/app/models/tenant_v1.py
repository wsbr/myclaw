"""Persisted Tenant authority for the minimum local data boundary."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.common import TimestampMixin, VersionMixin, utcnow, uuid7


DEFAULT_TENANT_ID = "00000000-0000-7000-8000-000000000001"
DEFAULT_TENANT_SLUG = "default"


class Tenant(Base, TimestampMixin, VersionMixin):
    __tablename__ = "tenant"
    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="ck_tenant_status"),
        UniqueConstraint("slug", name="uq_tenant_slug"),
        Index("ix_tenant_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)


class TenantMembership(Base, TimestampMixin, VersionMixin):
    """One account's authority inside its immutable local Tenant boundary."""

    __tablename__ = "tenant_membership"
    __table_args__ = (
        CheckConstraint(
            "role_key IN ('member','admin')", name="ck_tenant_membership_role"
        ),
        CheckConstraint(
            "status IN ('active','disabled')", name="ck_tenant_membership_status"
        ),
        UniqueConstraint(
            "tenant_id", "account_id", name="uq_tenant_membership_account"
        ),
        Index(
            "ix_tenant_membership_account_status",
            "account_id", "status", "tenant_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant.id"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_account.id"), nullable=False
    )
    role_key: Mapped[str] = mapped_column(String(20), default="member", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)

