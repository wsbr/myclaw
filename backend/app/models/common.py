"""Shared persistence primitives for the phase-one domain model."""

from __future__ import annotations

import secrets
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    """Return a naive UTC value suitable for MySQL ``DATETIME(6)``."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def uuid7() -> str:
    """Generate an RFC 9562 UUIDv7 without requiring Python 3.14."""

    timestamp_ms = time.time_ns() // 1_000_000
    random_bits = secrets.randbits(74)
    value = (timestamp_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((random_bits >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= random_bits & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )


class TimestampMixin(CreatedAtMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, onupdate=utcnow, nullable=False
    )


class VersionMixin:
    version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
