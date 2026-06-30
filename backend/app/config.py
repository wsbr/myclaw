from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    database_url: str = "mysql+aiomysql://myclaw:mypassword@mysql:3306/myclaw"

    # Redis
    redis_url: str = "redis://:redispassword@redis:6379/0"

    # Security
    secret_key: str = "change-me-to-a-random-32-char-string"
    encryption_key: str = "change-me-to-32-byte-aes-key-hex0"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days

    # Celery
    celery_broker_url: str = "redis://:redispassword@redis:6379/1"
    celery_result_backend: str = "redis://:redispassword@redis:6379/2"

    # App
    debug: bool = False
    cors_origins: list[str] = ["http://localhost", "http://localhost:80"]

    # Initial admin
    init_admin_email: str = "admin@myclaw.com"
    init_admin_password: str = "changeme123"

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
