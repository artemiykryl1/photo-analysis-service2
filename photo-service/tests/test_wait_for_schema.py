"""Tests for `app.db.wait_for_schema` (TASK-003 C1, design §9.6).

Exercises the polling/timeout logic against a mocked async engine - no real
Postgres connection here (that would need Docker; the migrate Job itself is
exercised on a real, ephemeral Postgres by the Docker-gated
`test_migration_integration.py`, a different concern: creating the schema,
not waiting for it to appear).

All timeouts/intervals below are small (tens of milliseconds) so the whole
file runs in well under a second - the retry loop uses real `asyncio.sleep`
and real wall-clock time, deliberately not mocked, to keep the test honest
about the loop's actual behaviour.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.wait_for_schema import main, wait_for_schema


def _fake_engine(connect_outcomes):
    """Build a MagicMock standing in for an `AsyncEngine`.

    `connect_outcomes` is an iterable; each item is either `None` (the
    `SELECT 1 FROM alembic_version` succeeds) or an `Exception` instance
    (the query raises it) - consumed one per `engine.connect()` call, in
    order, mirroring one poll attempt each.
    """
    engine = MagicMock()
    engine.dispose = AsyncMock()

    iterator = iter(connect_outcomes)

    def _connect(*_args, **_kwargs):
        outcome = next(iterator)
        conn = AsyncMock()
        if isinstance(outcome, Exception):
            conn.execute = AsyncMock(side_effect=outcome)
        else:
            conn.execute = AsyncMock(return_value=None)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    engine.connect = MagicMock(side_effect=_connect)
    return engine


class TestWaitForSchema:
    async def test_returns_true_on_first_successful_query(self):
        engine = _fake_engine([None])

        with patch("app.db.wait_for_schema.create_async_engine", return_value=engine):
            result = await wait_for_schema("postgresql+asyncpg://x", timeout_seconds=5.0)

        assert result is True
        engine.connect.assert_called_once()
        engine.dispose.assert_awaited_once()

    async def test_retries_after_a_transient_failure_then_succeeds(self):
        engine = _fake_engine([RuntimeError('relation "alembic_version" does not exist'), None])

        with patch("app.db.wait_for_schema.create_async_engine", return_value=engine):
            result = await wait_for_schema(
                "postgresql+asyncpg://x", timeout_seconds=5.0, poll_interval_seconds=0.01
            )

        assert result is True
        assert engine.connect.call_count == 2
        engine.dispose.assert_awaited_once()

    async def test_times_out_and_returns_false_when_never_ready(self):
        # Way more outcomes than attempts fitting in the tiny timeout below -
        # the loop must stop on the deadline, not on running out of outcomes.
        engine = _fake_engine([RuntimeError("still missing")] * 50)

        with patch("app.db.wait_for_schema.create_async_engine", return_value=engine):
            result = await wait_for_schema(
                "postgresql+asyncpg://x", timeout_seconds=0.1, poll_interval_seconds=0.02
            )

        assert result is False
        engine.dispose.assert_awaited_once()

    async def test_engine_is_always_disposed_even_on_timeout(self):
        """Regression guard: a leaked engine/connection pool on timeout would
        be a resource leak in a short-lived initContainer that may retry."""
        engine = _fake_engine([RuntimeError("nope")] * 10)

        with patch("app.db.wait_for_schema.create_async_engine", return_value=engine):
            await wait_for_schema("postgresql+asyncpg://x", timeout_seconds=0.05)

        engine.dispose.assert_awaited_once()


class TestMain:
    def test_exits_1_when_schema_never_becomes_ready(self, monkeypatch):
        async def _fake_wait_for_schema(*_args, **_kwargs):
            return False

        monkeypatch.setattr("app.db.wait_for_schema.wait_for_schema", _fake_wait_for_schema)

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    def test_does_not_exit_when_schema_is_ready(self, monkeypatch):
        async def _fake_wait_for_schema(*_args, **_kwargs):
            return True

        monkeypatch.setattr("app.db.wait_for_schema.wait_for_schema", _fake_wait_for_schema)

        main()  # must return normally (no SystemExit)
