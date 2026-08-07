from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


pytestmark = pytest.mark.unit


def _production_settings(**overrides):
    values = {
        "myclaw_environment": "production",
        "secret_key": "unit-test-secret-that-is-not-a-default",
        "encryption_key": "0123456789abcdef0123456789abcdef",
        "init_admin_password": "unit-test-admin-password",
        "debug": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_settings_reject_unsafe_defaults(monkeypatch):
    for name in ("SECRET_KEY", "ENCRYPTION_KEY", "INIT_ADMIN_PASSWORD", "DEBUG"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError, match="unsafe production defaults"):
        Settings(myclaw_environment="production", _env_file=None)


def test_production_settings_reject_debug_mode():
    with pytest.raises(ValidationError, match="DEBUG must be false"):
        _production_settings(debug=True)


def test_runtime_heartbeat_must_be_shorter_than_runner_lease():
    with pytest.raises(ValidationError, match="HEARTBEAT_SECONDS must be below"):
        _production_settings(runtime_runner_lease_seconds=10, runtime_runner_heartbeat_seconds=10)


def test_sandbox_heartbeat_must_be_shorter_than_attempt_lease():
    with pytest.raises(ValidationError, match="SANDBOX_ATTEMPT_HEARTBEAT_SECONDS"):
        _production_settings(
            sandbox_attempt_lease_seconds=10,
            sandbox_attempt_heartbeat_seconds=10,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_pool_size", 0),
        ("database_max_overflow", -1),
        ("runtime_worker_poll_seconds", 0),
        ("runtime_outbox_claim_lease_seconds", 0),
        ("runtime_runner_lease_seconds", 0),
        ("runtime_runner_heartbeat_seconds", 0),
        ("sandbox_worker_poll_seconds", 0),
        ("sandbox_outbox_claim_lease_seconds", 0),
        ("sandbox_attempt_stale_seconds", 0),
        ("sandbox_attempt_lease_seconds", 0),
        ("sandbox_attempt_heartbeat_seconds", 0),
        ("sandbox_tool_max_attempts", 0),
        ("outbox_max_delivery_attempts", 0),
        ("rate_limit_requests", 0),
        ("rate_limit_unauthenticated_requests", 0),
        ("rate_limit_window_seconds", 0),
        ("maintenance_poll_seconds", 0),
        ("maintenance_batch_size", 0),
    ],
)
def test_worker_and_pool_tuning_rejects_non_positive_values(field: str, value: int):
    with pytest.raises(ValidationError):
        _production_settings(**{field: value})


def test_safe_production_settings_are_accepted():
    settings = _production_settings(
        database_pool_size=24,
        database_max_overflow=8,
        runtime_runner_lease_seconds=45,
        runtime_runner_heartbeat_seconds=12,
    )
    assert settings.database_pool_size == 24
    assert settings.runtime_runner_lease_seconds == 45


def test_uat_compose_uses_protected_persistent_volumes_and_secret_injection():
    repository = Path(__file__).resolve().parents[3]
    compose = (repository / "deploy/docker-compose.uat.yml").read_text()
    collector = (repository / "deploy/otel-collector.yaml").read_text()
    example = (repository / "deploy/uat/.env.example").read_text()

    assert "tmpfs:" not in compose
    for volume_name in (
        "myclaw-uat-mysql-data",
        "myclaw-uat-redis-data",
        "myclaw-uat-app-data",
    ):
        assert f"name: {volume_name}" in compose
    assert compose.count("external: true") == 3
    assert "MYCLAW_REAL_LLM_API_KEY: ${MYCLAW_REAL_LLM_API_KEY}" in compose
    assert compose.count("MYCLAW_REAL_LLM_API_KEY: ${MYCLAW_REAL_LLM_API_KEY}") == 2
    assert "--no-tablespaces" in (repository / "deploy/uat/backup.sh").read_text()
    assert "MYCLAW_REAL_LLM_API_KEY=\n" in example
    assert "env://MYCLAW_REAL_LLM_API_KEY" in example
    assert "install -d -m 0750 -o 10001 -g 10001 /var/lib/myclaw/otel" in compose
    assert "condition: service_completed_successfully" in compose
    assert "cap_drop: [ALL]" in compose
    assert "cap_add: [CHOWN, FOWNER]" in compose
    assert "no-new-privileges:true" in compose
    assert "chown -R" not in compose
    assert "path: /var/lib/myclaw/otel/uat-otel-traces.jsonl" in collector
    assert "exporters: [debug, file]" in collector


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("maintenance_batch_size", 5001),
        ("outbox_completed_retention_days", 29),
        ("outbox_dead_retention_days", 179),
        ("checkpoint_retention_days", 29),
        ("trace_retention_days", 0),
        ("delivery_attempt_retention_days", 89),
    ],
)
def test_retention_safety_floors_are_enforced(field: str, value: int):
    with pytest.raises(ValidationError):
        _production_settings(**{field: value})
