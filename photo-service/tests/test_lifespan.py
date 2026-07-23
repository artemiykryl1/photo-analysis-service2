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


async def test_lifespan_cancels_outbox_task_when_shutdown_exceeds_timeout():
    """tasks/TASK-002/20_design.md §4.3: if the outbox task does not
    finish within `OUTBOX_SHUTDOWN_TIMEOUT_SECONDS` after `stop_event.set()`
    (e.g. it is stuck awaiting a slow Kafka call), shutdown must cancel it
    rather than hang the whole process shutdown forever."""
    import asyncio

    fake_app = _FakeApp()

    async def _never_finishing_outbox_publisher(*_args, **_kwargs):
        await asyncio.Event().wait()  # never resolves on its own

    with patch.object(main_module, "ObjectStorage") as mock_storage_cls, patch.object(
        main_module, "engine"
    ) as mock_engine, patch.object(
        main_module, "OUTBOX_SHUTDOWN_TIMEOUT_SECONDS", 0.02
    ), patch.object(
        main_module, "run_outbox_publisher", side_effect=_never_finishing_outbox_publisher
    ), patch.object(
        main_module, "KafkaEventProducer"
    ) as mock_producer_cls:
        mock_storage_cls.return_value = MagicMock()
        conn = AsyncMock()
        mock_engine.connect.return_value.__aenter__.return_value = conn
        mock_engine.connect.return_value.__aexit__.return_value = False
        mock_engine.dispose = AsyncMock()

        fake_producer = AsyncMock()
        mock_producer_cls.return_value = fake_producer

        async with lifespan(fake_app):
            pass

        # Must have completed shutdown (did not hang) and cleaned up.
        assert fake_app.state.outbox_task.cancelled() or fake_app.state.outbox_task.done()
        fake_producer.stop.assert_awaited_once()
        mock_engine.dispose.assert_awaited_once()


async def test_lifespan_swallows_producer_stop_failure_and_still_disposes_engine():
    """A failure in `producer.stop()` must be logged and swallowed - it
    must not prevent `engine.dispose()` from running (constitution.md
    §3.2 graceful shutdown order must complete regardless)."""
    fake_app = _FakeApp()

    with patch.object(main_module, "ObjectStorage") as mock_storage_cls, patch.object(
        main_module, "engine"
    ) as mock_engine, patch.object(main_module, "KafkaEventProducer") as mock_producer_cls:
        mock_storage_cls.return_value = MagicMock()
        conn = AsyncMock()
        mock_engine.connect.return_value.__aenter__.return_value = conn
        mock_engine.connect.return_value.__aexit__.return_value = False
        mock_engine.dispose = AsyncMock()

        fake_producer = AsyncMock()
        fake_producer.stop.side_effect = RuntimeError("producer already closed")
        mock_producer_cls.return_value = fake_producer

        # Must not raise despite producer.stop() failing.
        async with lifespan(fake_app):
            pass

        fake_producer.stop.assert_awaited_once()
        mock_engine.dispose.assert_awaited_once()
