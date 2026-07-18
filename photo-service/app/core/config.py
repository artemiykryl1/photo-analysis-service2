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

    # Kafka (TASK-002: tasks/TASK-002/20_design.md §10.1)
    KAFKA_BOOTSTRAP_SERVERS: str = "kafka:9092"
    KAFKA_TOPIC_ANALYSIS_REQUESTED: str = "photo.analysis.requested"
    KAFKA_CONSUMER_GROUP: str = "photo-analysis-workers"
    KAFKA_PUBLISH_TIMEOUT_SECONDS: float = 10.0

    # Analyzer gRPC
    ANALYZER_GRPC_ADDR: str = "analyzer-stub:50051"
    ANALYZER_GRPC_TIMEOUT: float = 30.0

    # Worker retry/backoff (constitution.md §2.4)
    WORKER_MAX_ATTEMPTS: int = 3
    RETRY_BACKOFF_BASE_SECONDS: float = 1.0  # backoff sequence: 1,2,4s

    # Metrics
    WORKER_METRICS_PORT: int = 8001

    # Outbox publisher (upload works even when Kafka is down)
    OUTBOX_POLL_INTERVAL_SECONDS: float = 2.0
    OUTBOX_BATCH_SIZE: int = 100


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (avoid re-parsing env on every Depends)."""
    return Settings()
