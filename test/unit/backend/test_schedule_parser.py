"""Unit tests for schedule parser."""

import pytest
from datetime import datetime, timedelta, timezone

from app.core.schedule.parser import parse_at, parse_delay, resolve_scheduled_at

pytestmark = pytest.mark.unit


class TestParseDelay:
    def test_minutes(self):
        assert parse_delay("5m") == timedelta(minutes=5)

    def test_hours(self):
        assert parse_delay("2h") == timedelta(hours=2)

    def test_seconds(self):
        assert parse_delay("30s") == timedelta(seconds=30)

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_delay("five minutes")


class TestResolveScheduledAt:
    def test_delay_is_one_shot(self):
        base = datetime(2026, 1, 1, 12, 0, 0)
        at, cron = resolve_scheduled_at(delay="5m", base=base)
        assert at == base + timedelta(minutes=5)
        assert cron is None

    def test_cron_is_recurring(self):
        base = datetime(2026, 1, 1, 12, 0, 0)
        at, cron = resolve_scheduled_at(cron_expr="0 8 * * *", base=base)
        assert at > base
        assert cron == "0 8 * * *"

    def test_requires_one_of(self):
        with pytest.raises(ValueError):
            resolve_scheduled_at()

    def test_at_absolute_time(self):
        at, cron = resolve_scheduled_at(at="2026-07-10T20:00:00")
        assert at == datetime(2026, 7, 10, 20, 0, 0)
        assert cron is None

    def test_at_with_z_suffix(self):
        at, _ = resolve_scheduled_at(at="2026-07-10T12:00:00Z")
        expected = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc).astimezone(tz=None).replace(tzinfo=None)
        assert at == expected


class TestParseAt:
    def test_iso_local(self):
        assert parse_at("2026-07-10T20:00:00") == datetime(2026, 7, 10, 20, 0, 0)
