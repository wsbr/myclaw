import ipaddress

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
    # Product/deployment release identity. This is intentionally separate from
    # runtime_release_id, which versions the persisted Runner protocol.
    myclaw_release: str = "development"
    debug: bool = False
    cors_origins: list[str] = ["http://localhost", "http://localhost:80"]
    # Some controlled egress gateways (including the local UAT tunnel) use a
    # synthetic DNS range for public names. Keep this opt-in and empty by
    # default; literal IP URLs and local/internal hostnames remain prohibited.
    trusted_synthetic_dns_cidrs: list[str] = []
    sandbox_root: str = "/var/lib/myclaw/workspaces"
    knowledge_root: str = "/var/lib/myclaw/knowledge"
    knowledge_max_source_bytes: int = Field(
        default=20 * 1024 * 1024, gt=0, le=100 * 1024 * 1024,
    )
    knowledge_max_pdf_pages: int = Field(default=300, gt=0, le=2000)
    knowledge_max_chunks_per_base: int = Field(default=10_000, gt=0, le=100_000)
    knowledge_chunk_chars: int = Field(default=1200, ge=256, le=8000)
    knowledge_chunk_overlap_chars: int = Field(default=150, ge=0, le=2000)
    skill_package_root: str = "/var/lib/myclaw/skill-packages"
    shared_skill_root: str = "/var/lib/myclaw/shared-skills"
    skill_storage_backend: str = "local"
    skill_s3_endpoint_url: str | None = None
    skill_s3_region: str = "us-east-1"
    skill_s3_bucket: str = "myclaw-skills"
    skill_s3_access_key_id: str | None = None
    skill_s3_secret_access_key: str | None = None
    skill_s3_force_path_style: bool = True
    skill_max_package_bytes: int = 20 * 1024 * 1024
    skill_max_files: int = 2000
    skill_max_unpacked_bytes: int = 100 * 1024 * 1024
    skill_manifest_max_bytes: int = Field(default=256 * 1024, gt=0, le=1024 * 1024)
    skill_catalog_max_tokens: int = Field(default=600, ge=64, le=4096)
    skill_sync_worker_poll_seconds: float = Field(default=1.0, gt=0)
    skill_sync_claim_lease_seconds: int = Field(default=30, gt=0)
    skill_sync_reconcile_seconds: int = Field(default=3600, gt=0)

    # Durable worker production tuning. Heartbeats must remain comfortably
    # below the Runner lease so a transient scheduling pause does not fence a
    # healthy worker.
    runtime_worker_poll_seconds: float = Field(default=0.5, gt=0)
    # Number of concurrently active Runner coroutines in one Runtime Worker
    # process. Excess durable commands remain in the Outbox instead of being
    # claimed into an unbounded in-memory queue.
    runtime_worker_concurrency: int = Field(default=8, ge=1, le=256)
    worker_heartbeat_seconds: int = Field(default=10, gt=0)
    worker_heartbeat_stale_seconds: int = Field(default=35, gt=0)
    # A cached provider entry owns one async SDK client and therefore one
    # reusable HTTP connection pool. The cache is process-local and keyed by
    # provider type, endpoint, and credential fingerprint.
    runtime_provider_pool_max_entries: int = Field(default=32, ge=1, le=256)
    runtime_outbox_claim_lease_seconds: int = Field(default=30, gt=0)
    runtime_runner_lease_seconds: int = Field(default=30, gt=0)
    runtime_runner_heartbeat_seconds: int = Field(default=10, gt=0)
    sandbox_worker_poll_seconds: float = Field(default=0.5, gt=0)
    sandbox_outbox_claim_lease_seconds: int = Field(default=30, gt=0)
    sandbox_attempt_stale_seconds: int = Field(default=60, gt=0)
    sandbox_attempt_lease_seconds: int = Field(default=45, gt=0)
    sandbox_attempt_heartbeat_seconds: int = Field(default=10, gt=0)
    sandbox_tool_max_attempts: int = Field(default=3, gt=0, le=10)
    outbox_max_delivery_attempts: int = Field(default=12, gt=0, le=100)
    provider_conformance_suite_version: str = "agent-native-v2.2"
    runtime_release_id: str = "agent-native-v2"
    provider_adapter_version: str = "2"
    context_policy_version: str = "context.v2"
    compaction_policy_version: str = "compaction.v2"
    continuation_policy_version: str = "continuation.v2"
    # Every governed Group candidate is shown to the Chat model as a compact
    # id/name/description catalog. Refuse emergence when that complete catalog
    # cannot fit this bound; never make a semantic choice from a truncated set.
    emergent_group_max_candidates: int = Field(default=100, ge=2, le=500)
    # The model must select a bounded subset from that complete catalog.
    emergent_group_max_members: int = Field(default=64, ge=2, le=500)

    # Retention maintenance. Authoritative Session/Run/Event/Artifact rows are
    # not deleted by this job; only disposable operational records are pruned.
    maintenance_poll_seconds: int = Field(default=3600, gt=0)
    maintenance_command_poll_seconds: float = Field(default=1.0, gt=0)
    maintenance_command_claim_lease_seconds: int = Field(default=300, gt=0)
    maintenance_command_heartbeat_seconds: int = Field(default=30, gt=0)
    maintenance_batch_size: int = Field(default=500, gt=0, le=5000)
    outbox_completed_retention_days: int = Field(default=30, ge=30)
    outbox_dead_retention_days: int = Field(default=180, ge=180)
    checkpoint_retention_days: int = Field(default=30, ge=30)
    trace_retention_days: int = Field(default=30, ge=1)
    delivery_attempt_retention_days: int = Field(default=90, ge=90)
    worker_heartbeat_retention_days: int = Field(default=7, ge=1)

    # Initial admin
    init_admin_email: str = "admin@myclaw.com"
    init_admin_password: str = "changeme123"

    @model_validator(mode="after")
    def validate_production_safety(self):
        storage_backend = self.skill_storage_backend.casefold()
        if storage_backend not in {"local", "s3"}:
            raise ValueError("SKILL_STORAGE_BACKEND must be local or s3")
        self.skill_storage_backend = storage_backend
        if storage_backend == "s3":
            missing = [
                name for name, value in {
                    "SKILL_S3_BUCKET": self.skill_s3_bucket,
                    "SKILL_S3_ACCESS_KEY_ID": self.skill_s3_access_key_id,
                    "SKILL_S3_SECRET_ACCESS_KEY": self.skill_s3_secret_access_key,
                }.items() if not value
            ]
            if missing:
                raise ValueError(f"missing S3 Skill storage settings: {', '.join(missing)}")
        for cidr in self.trusted_synthetic_dns_cidrs:
            try:
                network = ipaddress.ip_network(cidr, strict=True)
            except ValueError as error:
                raise ValueError(
                    f"TRUSTED_SYNTHETIC_DNS_CIDRS contains invalid CIDR: {cidr}"
                ) from error
            if network.version not in {4, 6}:
                raise ValueError(
                    f"TRUSTED_SYNTHETIC_DNS_CIDRS contains unsupported CIDR: {cidr}"
                )
        if self.runtime_runner_heartbeat_seconds >= self.runtime_runner_lease_seconds:
            raise ValueError("RUNTIME_RUNNER_HEARTBEAT_SECONDS must be below RUNTIME_RUNNER_LEASE_SECONDS")
        if self.worker_heartbeat_seconds >= self.worker_heartbeat_stale_seconds:
            raise ValueError("WORKER_HEARTBEAT_SECONDS must be below WORKER_HEARTBEAT_STALE_SECONDS")
        if self.knowledge_chunk_overlap_chars >= self.knowledge_chunk_chars:
            raise ValueError(
                "KNOWLEDGE_CHUNK_OVERLAP_CHARS must be below KNOWLEDGE_CHUNK_CHARS"
            )
        if self.sandbox_attempt_heartbeat_seconds >= self.sandbox_attempt_lease_seconds:
            raise ValueError("SANDBOX_ATTEMPT_HEARTBEAT_SECONDS must be below SANDBOX_ATTEMPT_LEASE_SECONDS")
        if (
            self.maintenance_command_heartbeat_seconds
            >= self.maintenance_command_claim_lease_seconds
        ):
            raise ValueError(
                "MAINTENANCE_COMMAND_HEARTBEAT_SECONDS must be below "
                "MAINTENANCE_COMMAND_CLAIM_LEASE_SECONDS"
            )
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
