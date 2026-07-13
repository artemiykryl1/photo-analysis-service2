"""Tests that the FastAPI app assembles correctly and /readyz behaves as
designed, all without a real Postgres/MinIO connection.

`app.main.engine` (the module-level SQLAlchemy `AsyncEngine`) and
`app.state.storage` are monkeypatched with fakes so no real network I/O
happens. `AsyncEngine.connect` is a read-only descriptor on the real
engine instance, so we cannot monkeypatch `engine.connect` directly -
instead we replace the whole `app.main.engine` name with a lightweight
fake object exposing a compatible `connect()` async context manager.
`monkeypatch` restores the original attribute after every test, so
these tests do not leak state into others.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from app.api import photos as photos_router_module
from app.main import app


def _patch_db(monkeypatch, *, healthy: bool) -> None:
    @asynccontextmanager
    async def fake_connect():
        if not healthy:
            raise ConnectionRefusedError("db down")
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=None)
        yield conn

    fake_engine = MagicMock()
    fake_engine.connect = fake_connect
    monkeypatch.setattr("app.main.engine", fake_engine)


def _patch_storage(monkeypatch, *, healthy: bool) -> None:
    fake_storage = MagicMock()
    if healthy:
        fake_storage.ensure_bucket.return_value = None
    else:
        fake_storage.ensure_bucket.side_effect = ConnectionRefusedError("minio down")
    app.state.storage = fake_storage


class TestAppAssembly:
    def test_app_title_matches_service_name(self):
        assert app.title == "photo-service"

    def test_photos_router_is_mounted(self):
        # The photos router is included via `app.include_router(...)`, which
        # newer Starlette versions expose as an opaque `_IncludedRouter`
        # entry in `app.routes` (no direct `.path` attribute) - so we assert
        # on the router object itself rather than trying to enumerate paths
        # through `app.routes`.
        assert photos_router_module.router.prefix == "/api/v1/photos"
        included = [r for r in app.routes if type(r).__name__ == "_IncludedRouter"]
        assert included, "photos router must be included in the app"

    def test_healthz_and_readyz_routes_registered(self):
        # `app.openapi()["paths"]` flattens routes (including ones mounted
        # via include_router) reliably across Starlette versions, unlike
        # walking `app.routes` directly.
        schema = app.openapi()
        assert "/healthz" in schema["paths"]
        assert "/readyz" in schema["paths"]

    def test_openapi_schema_generates_without_error(self):
        schema = app.openapi()
        assert schema["info"]["title"] == "photo-service"
        assert "/healthz" in schema["paths"]
        assert "/readyz" in schema["paths"]

    def test_no_upload_routes_exist_yet(self):
        """Bootstrap scope: no POST /api/v1/photos endpoint should exist."""
        schema = app.openapi()
        assert "/api/v1/photos" not in schema["paths"]


class TestReadyz:
    async def test_readyz_returns_200_when_db_and_storage_healthy(self, client, monkeypatch):
        _patch_db(monkeypatch, healthy=True)
        _patch_storage(monkeypatch, healthy=True)

        response = await client.get("/readyz")

        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    async def test_readyz_returns_503_when_db_unavailable(self, client, monkeypatch):
        _patch_db(monkeypatch, healthy=False)
        _patch_storage(monkeypatch, healthy=True)

        response = await client.get("/readyz")

        assert response.status_code == 503

    async def test_readyz_returns_503_when_storage_unavailable(self, client, monkeypatch):
        _patch_db(monkeypatch, healthy=True)
        _patch_storage(monkeypatch, healthy=False)

        response = await client.get("/readyz")

        assert response.status_code == 503

    async def test_readyz_returns_503_when_both_unavailable(self, client, monkeypatch):
        _patch_db(monkeypatch, healthy=False)
        _patch_storage(monkeypatch, healthy=False)

        response = await client.get("/readyz")

        assert response.status_code == 503

    async def test_readyz_degraded_response_does_not_crash_process(self, client, monkeypatch):
        """A degraded dependency must yield a 503 response, not an
        unhandled exception propagating out of the ASGI app."""
        _patch_db(monkeypatch, healthy=False)
        _patch_storage(monkeypatch, healthy=False)

        # If this raised, httpx/ASGITransport would propagate the
        # exception instead of returning a response.
        response = await client.get("/readyz")
        assert response.status_code == 503
