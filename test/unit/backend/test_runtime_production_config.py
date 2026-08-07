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


def test_uat_single_node_uses_isolated_persistent_volumes_and_sha_bound_operations():
    repository = Path(__file__).resolve().parents[3]
    uat = repository / "deploy/uat_single_node"
    compose = (uat / "docker-compose.yml").read_text()
    collector = (repository / "deploy/otel-collector.yaml").read_text()
    example = (uat / ".env.example").read_text()
    init_env = (uat / "init-env.sh").read_text()
    acceptance = (uat / "run-acceptance.sh").read_text()
    observation = (uat / "observe.sh").read_text()

    assert "tmpfs:" not in compose
    for volume_name in (
        "myclaw-uat-single-node-mysql-data",
        "myclaw-uat-single-node-redis-data",
        "myclaw-uat-single-node-app-data",
    ):
        assert f"name: {volume_name}" in compose
    assert compose.count("external: true") == 3
    assert "MYCLAW_REAL_LLM_API_KEY: ${MYCLAW_REAL_LLM_API_KEY:?real model key is required}" in compose
    assert sum(
        line.lstrip().startswith("MYCLAW_REAL_LLM_API_KEY:")
        for line in compose.splitlines()
    ) == 2
    assert "deploy:\n      replicas: 2" in compose
    assert "--no-tablespaces" in (uat / "backup.sh").read_text()
    assert "MYCLAW_REAL_LLM_API_KEY=\n" in example
    assert "MYCLAW_PRICING_TIER_MAX_INPUT_TOKENS=128000" in example
    assert "Refusing to reuse existing UAT volume" in init_env
    assert "Refusing to accept a dirty workspace" in acceptance
    assert "MYCLAW_OBSERVE_SECONDS:-1800" in observation
    assert "MYCLAW_OBSERVE_INTERVAL_SECONDS:-30" in observation
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
