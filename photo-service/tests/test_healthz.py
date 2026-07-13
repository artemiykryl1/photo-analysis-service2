"""Smoke tests for GET /healthz (liveness).

/healthz must always return 200 and must never touch the database or
MinIO - it is a pure liveness probe. We assert this by monkeypatching the
DB engine and the app-state storage to always raise, and confirming the
response is unaffected.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.main import app


async def test_healthz_returns_200(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "photo-service"


async def test_healthz_body_matches_schema_exactly(client):
    response = await client.get("/healthz")
    assert response.json() == {"status": "ok", "service": "photo-service"}


async def test_healthz_content_type_is_json(client):
    response = await client.get("/healthz")
    assert response.headers["content-type"].startswith("application/json")


async def test_healthz_does_not_touch_db_or_storage(client, monkeypatch):
    """Even if DB/MinIO would blow up, /healthz must stay green."""

    class ExplodingConn:
        async def __aenter__(self):
            raise AssertionError("healthz must not open a DB connection")

        async def __aexit__(self, *exc_info):
            return False

    fake_engine = MagicMock()
    fake_engine.connect = lambda: ExplodingConn()
    monkeypatch.setattr("app.main.engine", fake_engine)

    exploding_storage = AsyncMock()
    exploding_storage.ensure_bucket.side_effect = AssertionError(
        "healthz must not call storage.ensure_bucket"
    )
    app.state.storage = exploding_storage

    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "photo-service"}


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
async def test_healthz_rejects_other_http_methods(client, method):
    response = await client.request(method, "/healthz")
    assert response.status_code == 405
