"""Stable platform-owned facts shared by prompts and runtime Tools."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


PLATFORM_TIMEZONE = "Asia/Shanghai"


def runtime_clock_snapshot(
    execution_started_at: datetime, *, timezone_name: str = PLATFORM_TIMEZONE,
) -> dict[str, Any]:
    """Freeze one authoritative local clock for the lifetime of a Runner."""

    value = execution_started_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(ZoneInfo(timezone_name))
    offset = local.strftime("%z")
    facts = {
        "source": "runner_execution_started_at",
        "timezone": timezone_name,
        "local_date": local.date().isoformat(),
        "local_datetime": local.isoformat(timespec="seconds"),
        "utc_offset": offset[:3] + ":" + offset[3:],
        "year": local.year,
    }
    facts["fact_hash"] = hashlib.sha256(json.dumps(
        facts, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return facts


def runtime_clock_system_rules(clock: dict[str, Any]) -> str:
    """Render platform authority without introducing another prompt role."""

    return (
        " Platform runtime clock (authoritative): current local date is "
        f"{clock['local_date']}; current local datetime is {clock['local_datetime']}; "
        f"timezone is {clock['timezone']} (UTC offset {clock['utc_offset']}). "
        "Resolve relative dates such as today, yesterday, this week, recently, "
        "and their Chinese equivalents against this clock, and use explicit dates "
        "when forming web searches or reporting time-sensitive facts."
    )
