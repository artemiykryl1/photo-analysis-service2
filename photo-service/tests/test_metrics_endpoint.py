"""Behavioural coverage of `GET /metrics` (`app.main.metrics`).

Design §8.4: the endpoint refreshes the `photos_pending` gauge from the DB
on every scrape and always returns the Prometheus text exposition format,
even if the refresh query fails/times out (a metrics scrape must never
500). `PhotoRepository.count_pending` is monkeypatched - no real Postgres
connection.
"""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_module
from app.db.session import get_session
from app.integrations.metrics import photos_pending
from app.main import app
from app.repositories.photo_repository import PhotoRepository


@pytest.fixture
async def app_client():
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


class TestMetricsEndpoint:
    async def test_returns_200_prometheus_text_format(self, app_client, monkeypatch):
        monkeypatch.setattr(PhotoRepository, "count_pending", AsyncMock(return_value=3))

        response = await app_client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")

    async def test_body_contains_declared_metric_names(self, app_client, monkeypatch):
        monkeypatch.setattr(PhotoRepository, "count_pending", AsyncMock(return_value=0))

        response = await app_client.get("/metrics")
        body = response.text

        for metric_name in (
            "photos_uploaded_total",
            "http_requests_total",
            "http_request_duration_seconds",
            "storage_upload_errors_total",
            "kafka_publish_errors_total",
            "photos_pending",
        ):
            assert metric_name in body, f"expected {metric_name} in /metrics output"

    async def test_photos_pending_gauge_reflects_repository_count(self, app_client, monkeypatch):
        monkeypatch.setattr(PhotoRepository, "count_pending", AsyncMock(return_value=42))

        response = await app_client.get("/metrics")

        assert "photos_pending 42.0" in response.text

    async def test_scrape_never_500s_when_db_query_fails(self, app_client, monkeypatch):
        monkeypatch.setattr(
            PhotoRepository, "count_pending", AsyncMock(side_effect=ConnectionError("db down"))
        )

        response = await app_client.get("/metrics")

        assert response.status_code == 200

    async def test_gauge_keeps_last_known_value_when_refresh_fails(self, app_client, monkeypatch):
        monkeypatch.setattr(PhotoRepository, "count_pending", AsyncMock(return_value=5))
        await app_client.get("/metrics")
        assert photos_pending._value.get() == 5

        monkeypatch.setattr(
            PhotoRepository, "count_pending", AsyncMock(side_effect=ConnectionError("db down"))
        )
        response = await app_client.get("/metrics")

        assert response.status_code == 200
        assert photos_pending._value.get() == 5  # unchanged, not reset to 0

    async def test_scrape_respects_the_db_timeout_budget(self, app_client, monkeypatch):
        """§8.4: a slow/hanging DB query must not hang the scrape - the
        gauge refresh is wrapped in `asyncio.timeout(METRICS_DB_TIMEOUT_SECONDS)`."""
        import asyncio

        monkeypatch.setattr(main_module, "METRICS_DB_TIMEOUT_SECONDS", 0.05)

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(10)
            return 0

        monkeypatch.setattr(PhotoRepository, "count_pending", _hang)

        response = await app_client.get("/metrics")

        assert response.status_code == 200
