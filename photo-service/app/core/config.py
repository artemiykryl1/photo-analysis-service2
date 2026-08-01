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

    # TASK-003 D1 (tasks/TASK-003/20_design.md §2): the real analyzer at
    # 45.132.19.101:50051 hard-caps its gRPC message size at exactly 4 MiB
    # (spike A3, tasks/TASK-003/05_spike_analyzer.md finding 1) - it is the
    # server side that enforces this, so raising a client-side limit cannot
    # help. `ANALYZER_MAX_MESSAGE_BYTES` is only used to validate our own
    # prepared payload against that server ceiling before sending.
    ANALYZER_MAX_MESSAGE_BYTES: int = 4 * 1024 * 1024
    # Budget for `image_bytes` alone (~83% of the 4 MiB ceiling) - the
    # remainder covers `photo_id`/`object_key`/protobuf framing (< 150 bytes
    # in practice per design §2) with a deliberately generous safety margin,
    # since re-encoded JPEG size is not perfectly predictable.
    ANALYZER_MAX_IMAGE_BYTES: int = 3_500_000
    # Longest side (px) below which the original is sent unmodified; above
    # it, `image_prep.prepare_for_analysis` downscales via the fixed ladder
    # (design §2).
    ANALYZER_MAX_IMAGE_SIDE: int = 2048
    # Decompression-bomb guard: reject (without decoding) any image whose
    # header-reported pixel count exceeds this, per design §2/§5.4.
    ANALYZER_MAX_IMAGE_PIXELS: int = 50_000_000
    # MinIO read timeout for the worker's `ObjectStorage.get_file` call
    # (constitution.md §3.2: 60s for MinIO).
    STORAGE_READ_TIMEOUT_SECONDS: float = 60.0
    # Upper bound on CPU time for `prepare_for_analysis` (decode/resize/
    # encode) - a pathological input could otherwise consume minutes of CPU
    # (design §5.3).
    IMAGE_PREP_TIMEOUT_SECONDS: float = 30.0
    # After exhausting retries on UNAVAILABLE/DEADLINE_EXCEEDED, the worker
    # pauses this long before returning - a simple throttle so a lengthy
    # analyzer outage doesn't turn into a hot-loop against a shared external
    # service (design §12, A7 "вежливость").
    ANALYZER_UNAVAILABLE_COOLDOWN_SECONDS: float = 5.0

    # CORS (TASK-003 B3): explicit allow-list, never "*" - no auth exists
    # yet, so a wildcard origin would be actively misleading rather than
    # just permissive (design §8.5).
    CORS_ALLOWED_ORIGINS: str = "http://localhost:8080,http://localhost:5173"

    # TASK-003 C1 (tasks/TASK-003/20_design.md §9.6): upper bound for
    # `app.db.wait_for_schema` - the initContainer used by the api/worker
    # Kubernetes Deployments to block until the `30-migrate-job.yaml` Job
    # has created `alembic_version` (kubectl apply gives no ordering
    # guarantee between manifests). 120s comfortably covers a cold Postgres
    # StatefulSet start plus the migration itself.
    WAIT_FOR_SCHEMA_TIMEOUT_SECONDS: float = 120.0

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        """Parse the comma-separated `CORS_ALLOWED_ORIGINS` into a clean
        list (stripped, empty entries dropped) for `CORSMiddleware`."""
        return [origin.strip() for origin in self.CORS_ALLOWED_ORIGINS.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (avoid re-parsing env on every Depends)."""
    return Settings()
