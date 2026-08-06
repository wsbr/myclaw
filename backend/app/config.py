from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = "mysql+aiomysql://myclaw:mypassword@mysql:3306/myclaw"
    database_pool_size: int = Field(default=10, ge=1)
    database_max_overflow: int = Field(default=20, ge=0)

    # Redis
    redis_url: str = "redis://:redispassword@redis:6379/0"

    # Security
    secret_key: str = "change-me-to-a-random-32-char-string"
    encryption_key: str = "change-me-to-32-byte-aes-key-hex0"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=600, gt=0)
    rate_limit_unauthenticated_requests: int = Field(default=60, gt=0)
    rate_limit_window_seconds: int = Field(default=60, gt=0)

    # Enterprise identity assertion bridge. Deployments configure their IdP to
    # issue short-lived HS256 assertions with aud=myclaw-enterprise-login.
    enterprise_login_url: str | None = None
    enterprise_assertion_secret: str | None = None
    enterprise_assertion_issuer: str | None = None

    # App
    myclaw_environment: str = "development"
    debug: bool = False
    cors_origins: list[str] = ["http://localhost", "http://localhost:80"]
    sandbox_root: str = "/var/lib/myclaw/workspaces"
    skill_package_root: str = "/var/lib/myclaw/skill-packages"
    shared_skill_root: str = "/var/lib/myclaw/shared-skills"
    skill_max_package_bytes: int = 20 * 1024 * 1024
    skill_max_files: int = 2000
    skill_max_unpacked_bytes: int = 100 * 1024 * 1024

    # Durable worker production tuning. Heartbeats must remain comfortably
    # below the Runner lease so a transient scheduling pause does not fence a
    # healthy worker.
    runtime_worker_poll_seconds: float = Field(default=0.5, gt=0)
    runtime_outbox_claim_lease_seconds: int = Field(default=30, gt=0)
    runtime_runner_lease_seconds: int = Field(default=30, gt=0)
    runtime_runner_heartbeat_seconds: int = Field(default=10, gt=0)
    sandbox_worker_poll_seconds: float = Field(default=0.5, gt=0)
    sandbox_outbox_claim_lease_seconds: int = Field(default=30, gt=0)
    sandbox_attempt_stale_seconds: int = Field(default=60, gt=0)
    provider_conformance_suite_version: str = "agent-native-v2.2"
    runtime_release_id: str = "agent-native-v2"
    provider_adapter_version: str = "2"
    context_policy_version: str = "context.v2"
    compaction_policy_version: str = "compaction.v2"
    continuation_policy_version: str = "continuation.v2"

    # Retention maintenance. Authoritative Session/Run/Event/Artifact rows are
    # not deleted by this job; only disposable operational records are pruned.
    maintenance_poll_seconds: int = Field(default=3600, gt=0)
    maintenance_batch_size: int = Field(default=500, gt=0, le=5000)
    outbox_completed_retention_days: int = Field(default=30, ge=30)
    outbox_dead_retention_days: int = Field(default=180, ge=180)
    checkpoint_retention_days: int = Field(default=30, ge=30)
    trace_retention_days: int = Field(default=30, ge=1)
    delivery_attempt_retention_days: int = Field(default=90, ge=90)

    # Initial admin
    init_admin_email: str = "admin@myclaw.com"
    init_admin_password: str = "changeme123"

    @model_validator(mode="after")
    def validate_production_safety(self):
        if self.runtime_runner_heartbeat_seconds >= self.runtime_runner_lease_seconds:
            raise ValueError("RUNTIME_RUNNER_HEARTBEAT_SECONDS must be below RUNTIME_RUNNER_LEASE_SECONDS")
        if self.myclaw_environment.lower() == "production":
            unsafe = {
                "SECRET_KEY": self.secret_key.startswith("change-me"),
                "ENCRYPTION_KEY": self.encryption_key.startswith("change-me"),
                "INIT_ADMIN_PASSWORD": self.init_admin_password == "changeme123",
            }
            invalid = [name for name, is_unsafe in unsafe.items() if is_unsafe]
            if invalid:
                raise ValueError(f"unsafe production defaults: {', '.join(invalid)}")
            if self.debug:
                raise ValueError("DEBUG must be false in production")
        return self

@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
