"""Tests for `app.core.config.Settings`.

`Settings` (pydantic-settings) is the single source of configuration. We
verify: (a) sane bootstrap defaults exist so `docker compose up` works
without a `.env` file, (b) every env var actually overrides its default,
(c) secret-bearing fields do NOT default to a "production-looking" value
that could be mistaken for a real secret, and (d) an on-disk `.env` file
is not silently picked up from the developer's machine during tests
(model_config disables it here by pointing at a nonexistent file).

We never mutate `os.environ` directly - `monkeypatch.setenv` guarantees
env vars are restored after each test regardless of pass/fail, and we
avoid the process-wide `get_settings()` lru_cache by instantiating
`Settings` directly.
"""

from app.core.config import Settings, get_settings


def _settings_from_env(monkeypatch, **env: str) -> Settings:
    """Build a Settings instance from explicit env vars only.

    `_env_file=None` disables reading a real `.env` file from disk so the
    test is hermetic and does not depend on what happens to exist in the
    working directory.
    """
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestDefaults:
    def test_defaults_are_compose_friendly(self, monkeypatch):
        # Ensure no leftover env vars from the outer shell leak into this test.
        for key in [
            "DATABASE_URL",
            "MINIO_ENDPOINT",
            "MINIO_ACCESS_KEY",
            "MINIO_SECRET_KEY",
            "MINIO_BUCKET",
            "MINIO_SECURE",
            "LOG_LEVEL",
            "API_HOST",
            "API_PORT",
        ]:
            monkeypatch.delenv(key, raising=False)

        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert settings.APP_NAME == "photo-service"
        assert settings.API_HOST == "0.0.0.0"
        assert settings.API_PORT == 8000
        assert settings.LOG_LEVEL == "INFO"
        assert settings.DATABASE_URL.startswith("postgresql+asyncpg://")
        assert "postgres" in settings.DATABASE_URL  # compose hostname
        assert settings.MINIO_ENDPOINT == "minio:9000"  # compose hostname
        assert settings.MINIO_BUCKET == "photos"
        assert settings.MINIO_SECURE is False

    def test_minio_secret_defaults_are_local_dev_placeholders_only(self, monkeypatch):
        """The only acceptable "default secret" is the well-known MinIO
        local dev credential - it must not look like a real generated
        production secret, and must be trivially overridable via env.
        """
        monkeypatch.delenv("MINIO_ACCESS_KEY", raising=False)
        monkeypatch.delenv("MINIO_SECRET_KEY", raising=False)

        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert settings.MINIO_ACCESS_KEY == "minioadmin"
        assert settings.MINIO_SECRET_KEY == "minioadmin"


class TestEnvOverrides:
    def test_database_url_overridden_by_env(self, monkeypatch):
        settings = _settings_from_env(
            monkeypatch,
            DATABASE_URL="postgresql+asyncpg://u:p@custom-host:5432/db",
        )
        assert settings.DATABASE_URL == "postgresql+asyncpg://u:p@custom-host:5432/db"

    def test_minio_settings_overridden_by_env(self, monkeypatch):
        settings = _settings_from_env(
            monkeypatch,
            MINIO_ENDPOINT="s3.example.com:9000",
            MINIO_ACCESS_KEY="prod-access-key",
            MINIO_SECRET_KEY="prod-secret-key",
            MINIO_BUCKET="prod-photos",
            MINIO_SECURE="true",
        )
        assert settings.MINIO_ENDPOINT == "s3.example.com:9000"
        assert settings.MINIO_ACCESS_KEY == "prod-access-key"
        assert settings.MINIO_SECRET_KEY == "prod-secret-key"
        assert settings.MINIO_BUCKET == "prod-photos"
        assert settings.MINIO_SECURE is True

    def test_log_level_overridden_by_env(self, monkeypatch):
        settings = _settings_from_env(monkeypatch, LOG_LEVEL="DEBUG")
        assert settings.LOG_LEVEL == "DEBUG"

    def test_api_host_and_port_overridden_by_env(self, monkeypatch):
        settings = _settings_from_env(monkeypatch, API_HOST="127.0.0.1", API_PORT="9999")
        assert settings.API_HOST == "127.0.0.1"
        assert settings.API_PORT == 9999  # coerced to int

    def test_unknown_env_vars_are_ignored_not_rejected(self, monkeypatch):
        """extra="ignore" must hold - unrelated env vars must not raise."""
        monkeypatch.setenv("SOME_UNRELATED_VARIABLE", "whatever")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.APP_NAME == "photo-service"


class TestGetSettingsCaching:
    def test_get_settings_returns_cached_singleton(self):
        first = get_settings()
        second = get_settings()
        assert first is second


class TestTaskO02Defaults:
    """tasks/TASK-002/20_design.md §10.1 - new Kafka/analyzer/worker/outbox
    fields must have compose-friendly defaults so `docker compose up`
    works without a `.env` file."""

    def test_kafka_defaults(self, monkeypatch):
        for key in (
            "KAFKA_BOOTSTRAP_SERVERS",
            "KAFKA_TOPIC_ANALYSIS_REQUESTED",
            "KAFKA_CONSUMER_GROUP",
            "KAFKA_PUBLISH_TIMEOUT_SECONDS",
        ):
            monkeypatch.delenv(key, raising=False)

        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert settings.KAFKA_BOOTSTRAP_SERVERS == "kafka:9092"
        assert settings.KAFKA_TOPIC_ANALYSIS_REQUESTED == "photo.analysis.requested"
        assert settings.KAFKA_CONSUMER_GROUP == "photo-analysis-workers"
        assert settings.KAFKA_PUBLISH_TIMEOUT_SECONDS == 10.0

    def test_analyzer_grpc_defaults(self, monkeypatch):
        for key in ("ANALYZER_GRPC_ADDR", "ANALYZER_GRPC_TIMEOUT"):
            monkeypatch.delenv(key, raising=False)

        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert settings.ANALYZER_GRPC_ADDR == "analyzer-stub:50051"
        assert settings.ANALYZER_GRPC_TIMEOUT == 30.0

    def test_worker_retry_defaults(self, monkeypatch):
        for key in ("WORKER_MAX_ATTEMPTS", "RETRY_BACKOFF_BASE_SECONDS"):
            monkeypatch.delenv(key, raising=False)

        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert settings.WORKER_MAX_ATTEMPTS == 3
        assert settings.RETRY_BACKOFF_BASE_SECONDS == 1.0

    def test_outbox_and_metrics_defaults(self, monkeypatch):
        for key in ("OUTBOX_POLL_INTERVAL_SECONDS", "OUTBOX_BATCH_SIZE", "WORKER_METRICS_PORT"):
            monkeypatch.delenv(key, raising=False)

        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert settings.OUTBOX_POLL_INTERVAL_SECONDS == 2.0
        assert settings.OUTBOX_BATCH_SIZE == 100
        assert settings.WORKER_METRICS_PORT == 8001

    def test_kafka_and_worker_settings_overridden_by_env(self, monkeypatch):
        settings = _settings_from_env(
            monkeypatch,
            KAFKA_BOOTSTRAP_SERVERS="custom-kafka:9092",
            ANALYZER_GRPC_ADDR="custom-analyzer:50051",
            WORKER_MAX_ATTEMPTS="5",
            OUTBOX_POLL_INTERVAL_SECONDS="0.5",
        )

        assert settings.KAFKA_BOOTSTRAP_SERVERS == "custom-kafka:9092"
        assert settings.ANALYZER_GRPC_ADDR == "custom-analyzer:50051"
        assert settings.WORKER_MAX_ATTEMPTS == 5
        assert settings.OUTBOX_POLL_INTERVAL_SECONDS == 0.5
