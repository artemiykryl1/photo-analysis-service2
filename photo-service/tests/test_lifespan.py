"""Tests for `app.main.lifespan` (startup/shutdown).

`httpx.ASGITransport` does not trigger ASGI lifespan events, so
`lifespan()` is exercised directly here as a plain async context
manager, with `ObjectStorage` and the DB engine mocked - no real MinIO
or Postgres connection is made. This proves the degradation contract:
startup must never crash the process when MinIO or Postgres are
unavailable (constitution.md - graceful startup), and shutdown must
release the DB engine.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import app.main as main_module
from app.main import lifespan


class _FakeApp:
    """Minimal stand-in for FastAPI app - only needs a `.state` bucket."""

    def __init__(self):
        self.state = MagicMock()


async def test_lifespan_succeeds_when_db_and_storage_healthy():
    fake_app = _FakeApp()

    with patch.object(main_module, "ObjectStorage") as mock_storage_cls, patch.object(
        main_module, "engine"
    ) as mock_engine:
        mock_storage_instance = MagicMock()
        mock_storage_cls.return_value = mock_storage_instance

        conn = AsyncMock()
        mock_engine.connect.return_value.__aenter__.return_value = conn
        mock_engine.connect.return_value.__aexit__.return_value = False
        mock_engine.dispose = AsyncMock()

        async with lifespan(fake_app):
            mock_storage_instance.ensure_bucket.assert_called_once()
            conn.execute.assert_awaited_once()
            assert fake_app.state.storage is mock_storage_instance

        mock_engine.dispose.assert_awaited_once()


async def test_lifespan_does_not_raise_when_minio_unavailable():
    """Startup must not crash the process if MinIO is unreachable."""
    fake_app = _FakeApp()

    with patch.object(main_module, "ObjectStorage") as mock_storage_cls, patch.object(
        main_module, "engine"
    ) as mock_engine:
        mock_storage_instance = MagicMock()
        mock_storage_instance.ensure_bucket.side_effect = ConnectionRefusedError("minio down")
        mock_storage_cls.return_value = mock_storage_instance

        conn = AsyncMock()
        mock_engine.connect.return_value.__aenter__.return_value = conn
        mock_engine.connect.return_value.__aexit__.return_value = False
        mock_engine.dispose = AsyncMock()

        # Must not raise despite ensure_bucket failing.
        async with lifespan(fake_app):
            assert fake_app.state.storage is mock_storage_instance

        mock_engine.dispose.assert_awaited_once()


async def test_lifespan_does_not_raise_when_db_unavailable():
    """Startup must not crash the process if Postgres is unreachable."""
    fake_app = _FakeApp()

    with patch.object(main_module, "ObjectStorage") as mock_storage_cls, patch.object(
        main_module, "engine"
    ) as mock_engine:
        mock_storage_instance = MagicMock()
        mock_storage_cls.return_value = mock_storage_instance

        mock_engine.connect.side_effect = ConnectionRefusedError("db down")
        mock_engine.dispose = AsyncMock()

        # Must not raise despite the DB connection attempt failing.
        async with lifespan(fake_app):
            pass

        mock_engine.dispose.assert_awaited_once()


async def test_lifespan_disposes_engine_on_shutdown_even_after_body_runs():
    fake_app = _FakeApp()

    with patch.object(main_module, "ObjectStorage") as mock_storage_cls, patch.object(
        main_module, "engine"
    ) as mock_engine:
        mock_storage_cls.return_value = MagicMock()
        conn = AsyncMock()
        mock_engine.connect.return_value.__aenter__.return_value = conn
        mock_engine.connect.return_value.__aexit__.return_value = False
        mock_engine.dispose = AsyncMock()

        async with lifespan(fake_app):
            pass

        mock_engine.dispose.assert_awaited_once()
