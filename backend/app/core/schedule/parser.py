"""Parse relative delays and cron expressions for scheduled tasks."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from croniter import croniter

_DELAY_RE = re.compile(
    r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
    re.IGNORECASE,
)


def parse_delay(delay: str) -> timedelta:
    text = delay.strip().lower().replace(" ", "")
    match = _DELAY_RE.match(text)
    if not match:
        raise ValueError(f"Invalid delay: {delay!r} (expected e.g. 5m, 1h, 30s)")

    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit in ("s", "sec", "secs", "second", "seconds"):
        return timedelta(seconds=amount)
    if unit in ("m", "min", "mins", "minute", "minutes"):
        return timedelta(minutes=amount)
    if unit in ("h", "hr", "hrs", "hour", "hours"):
        return timedelta(hours=amount)
    return timedelta(days=amount)


def parse_at(at: str) -> datetime:
    text = at.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz=None).replace(tzinfo=None)
    return dt


def resolve_scheduled_at(
    *,
    delay: str | None = None,
    cron_expr: str | None = None,
    at: str | None = None,
    base: datetime | None = None,
) -> tuple[datetime, str | None]:
    """Return (scheduled_at, cron_expr). cron_expr is set only for recurring schedules."""
    now = base or datetime.utcnow()

    if at:
        return parse_at(at), cron_expr

    if delay:
        return now + parse_delay(delay), None

    if cron_expr:
        try:
            next_run = croniter(cron_expr, now).get_next(datetime)
        except Exception as exc:
            raise ValueError(f"Invalid cron expression: {cron_expr!r}") from exc
        return next_run, cron_expr

    raise ValueError("One of delay, cron_expr, or at is required")
