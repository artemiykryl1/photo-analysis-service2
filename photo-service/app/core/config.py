"""Application configuration.

Single source of truth for configuration, loaded from environment variables
and (locally) from a `.env` file. Defaults are tuned for the docker-compose
setup (hostnames `postgres`/`minio`). Real secrets are NEVER committed and
must be provided via env vars / secret manager in production; the
MinIO credentials default to `minioadmin` for local development only.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # App
    APP_NAME: str = "photo-service"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://photo:photo@postgres:5432/photo_db"

    # MinIO
    MINIO_ENDPOINT: str = "minio:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"  # local default only; prod must override via env/secret
    MINIO_SECRET_KEY: str = "minioadmin"  # local default only; prod must override via env/secret
    MINIO_BUCKET: str = "photos"
    MINIO_SECURE: bool = False  # http locally


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (avoid re-parsing env on every Depends)."""
    return Settings()
