"""Behavioural coverage of `app.services.outbox.run_outbox_publisher`.

Design §4.2 / §13.2 "Outbox-переходы": `not_sent -> sent` on a successful
publish, row stays `not_sent` (and `kafka_publish_errors_total` increments)
when `producer.publish_analysis_requested` raises, and the loop
self-heals via `producer.ensure_started()` on every iteration (review-1
BLK-1) instead of crashing when Kafka is down at the time of a poll.

`session_factory` and `producer` are always fakes/mocks - no real
Postgres/Kafka connection is made. `stop_event` is pre-set (or set inside a
fake awaitable) so each test runs exactly one iteration of the loop instead
of looping forever waiting for `OUTBOX_POLL_INTERVAL_SECONDS`.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiokafka.errors import KafkaError

from app.core.config import Settings
from app.integrations import metrics_api as metrics_module
from app.services.outbox import run_outbox_publisher


def _settings() -> Settings:
    return Settings(OUTBOX_POLL_INTERVAL_SECONDS=0.01, OUTBOX_BATCH_SIZE=100)


def _fake_row(*, photo_id=None, object_key="photos/x/original.jpg", trace_id="trace-1"):
    row = MagicMock()
    row.photo_id = photo_id or uuid.uuid4()
    row.object_key = object_key
    row.created_at = datetime.now(timezone.utc)
    row.trace_id = trace_id
    return row


class _OneShotStopEvent:
    """A stand-in `asyncio.Event` that reports "set" only after the loop
    body has run once - lets `run_outbox_publisher` execute exactly one
    iteration before exiting, without depending on real wall-clock sleeps."""

    def __init__(self, iterations: int = 1):
        self._remaining = iterations
        self._real_event = asyncio.Event()

    def is_set(self) -> bool:
        if self._remaining <= 0:
            return True
        return False

    async def wait(self):
        self._remaining -= 1
        if self._remaining <= 0:
            return True
        # Never resolves on its own within the test's timeframe - the
        # `asyncio.wait_for(..., timeout=...)` wrapper in the publisher
        # will time out and loop back to `is_set()`.
        await self._real_event.wait()


def _session_factory(session: AsyncMock):
    @asynccontextmanager
    async def _factory():
        yield session

    return _factory


class TestSuccessfulPublish:
    async def test_not_sent_row_is_published_and_marked_sent(self):
        row = _fake_row()
        session = AsyncMock()
        with pytest.MonkeyPatch.context() as mp:
            repo_instance = AsyncMock()
            repo_instance.fetch_unpublished.return_value = [row]
            mp.setattr("app.services.outbox.PhotoRepository", lambda: repo_instance)

            producer = AsyncMock()
            producer.ensure_started = AsyncMock()
            producer.publish_analysis_requested = AsyncMock()

            await run_outbox_publisher(
                _session_factory(session), producer, _settings(), _OneShotStopEvent()
            )

            producer.ensure_started.assert_awaited_once()
            producer.publish_analysis_requested.assert_awaited_once_with(
                str(row.photo_id), row.object_key, row.created_at, row.trace_id
            )
            repo_instance.mark_published.assert_awaited_once_with(session, row.photo_id)
            session.commit.assert_awaited_once()

    async def test_row_without_trace_id_gets_a_generated_uuid(self):
        row = _fake_row(trace_id=None)
        session = AsyncMock()
        with pytest.MonkeyPatch.context() as mp:
            repo_instance = AsyncMock()
            repo_instance.fetch_unpublished.return_value = [row]
            mp.setattr("app.services.outbox.PhotoRepository", lambda: repo_instance)

            producer = AsyncMock()

            await run_outbox_publisher(
                _session_factory(session), producer, _settings(), _OneShotStopEvent()
            )

            call = producer.publish_analysis_requested.call_args
            generated_trace_id = call.args[3]
            uuid.UUID(generated_trace_id)  # must be a valid uuid string, not None


class TestPublishFailureLeavesRowUnsent:
    async def test_kafka_error_on_publish_leaves_row_not_sent_and_increments_metric(self):
        row = _fake_row()
        session = AsyncMock()
        before = metrics_module.kafka_publish_errors_total._value.get()

        with pytest.MonkeyPatch.context() as mp:
            repo_instance = AsyncMock()
            repo_instance.fetch_unpublished.return_value = [row]
            mp.setattr("app.services.outbox.PhotoRepository", lambda: repo_instance)

            producer = AsyncMock()
            producer.publish_analysis_requested.side_effect = KafkaError("broker down")

            await run_outbox_publisher(
                _session_factory(session), producer, _settings(), _OneShotStopEvent()
            )

            repo_instance.mark_published.assert_not_called()
            # the loop must not crash - it still commits the (no-op) transaction
            session.commit.assert_awaited_once()

        after = metrics_module.kafka_publish_errors_total._value.get()
        assert after == before + 1

    async def test_timeout_error_on_publish_also_leaves_row_not_sent(self):
        row = _fake_row()
        session = AsyncMock()
        with pytest.MonkeyPatch.context() as mp:
            repo_instance = AsyncMock()
            repo_instance.fetch_unpublished.return_value = [row]
            mp.setattr("app.services.outbox.PhotoRepository", lambda: repo_instance)

            producer = AsyncMock()
            producer.publish_analysis_requested.side_effect = TimeoutError("slow broker")

            await run_outbox_publisher(
                _session_factory(session), producer, _settings(), _OneShotStopEvent()
            )

            repo_instance.mark_published.assert_not_called()

    async def test_one_failing_row_does_not_stop_the_rest_of_the_batch(self):
        good_row = _fake_row()
        bad_row = _fake_row()
        session = AsyncMock()
        with pytest.MonkeyPatch.context() as mp:
            repo_instance = AsyncMock()
            repo_instance.fetch_unpublished.return_value = [bad_row, good_row]
            mp.setattr("app.services.outbox.PhotoRepository", lambda: repo_instance)

            producer = AsyncMock()

            async def _publish(photo_id, *_args, **_kwargs):
                if photo_id == str(bad_row.photo_id):
                    raise KafkaError("flaky send")

            producer.publish_analysis_requested.side_effect = _publish

            await run_outbox_publisher(
                _session_factory(session), producer, _settings(), _OneShotStopEvent()
            )

            repo_instance.mark_published.assert_awaited_once_with(session, good_row.photo_id)


class TestProducerNotStarted:
    async def test_ensure_started_failure_skips_iteration_without_crashing(self):
        session = AsyncMock()
        with pytest.MonkeyPatch.context() as mp:
            repo_instance = AsyncMock()
            mp.setattr("app.services.outbox.PhotoRepository", lambda: repo_instance)

            producer = AsyncMock()
            producer.ensure_started.side_effect = ConnectionError("kafka not reachable")

            # Must not raise - self-heals on the next poll (design §4.2 note).
            await run_outbox_publisher(
                _session_factory(session), producer, _settings(), _OneShotStopEvent()
            )

            repo_instance.fetch_unpublished.assert_not_called()


class TestEmptyOutbox:
    async def test_no_unpublished_rows_is_a_noop_commit(self):
        session = AsyncMock()
        with pytest.MonkeyPatch.context() as mp:
            repo_instance = AsyncMock()
            repo_instance.fetch_unpublished.return_value = []
            mp.setattr("app.services.outbox.PhotoRepository", lambda: repo_instance)

            producer = AsyncMock()

            await run_outbox_publisher(
                _session_factory(session), producer, _settings(), _OneShotStopEvent()
            )

            producer.publish_analysis_requested.assert_not_called()
            session.commit.assert_awaited_once()


class TestStopEventHonoured:
    async def test_loop_exits_immediately_when_stop_event_already_set(self):
        already_set = MagicMock()
        already_set.is_set.return_value = True
        producer = AsyncMock()

        await run_outbox_publisher(
            _session_factory(AsyncMock()), producer, _settings(), already_set
        )

        producer.ensure_started.assert_not_called()


class _NeverResolvingWaitStopEvent:
    """`is_set()` reports not-set for `iterations` calls, then set;
    `wait()` never resolves on its own - forces `asyncio.wait_for(...,
    timeout=...)` in the publisher to genuinely time out, exercising the
    `except TimeoutError: pass` branch (the ordinary "no shutdown signal
    within the poll interval" case)."""

    def __init__(self, iterations: int = 1):
        self._calls = 0
        self._iterations = iterations

    def is_set(self) -> bool:
        self._calls += 1
        return self._calls > self._iterations

    async def wait(self):
        await asyncio.Event().wait()  # blocks forever - times out via wait_for


class TestPollIntervalTimeoutBranch:
    async def test_wait_for_timeout_between_polls_is_the_normal_case(self):
        session = AsyncMock()
        with pytest.MonkeyPatch.context() as mp:
            repo_instance = AsyncMock()
            repo_instance.fetch_unpublished.return_value = []
            mp.setattr("app.services.outbox.PhotoRepository", lambda: repo_instance)

            producer = AsyncMock()
            settings = Settings(OUTBOX_POLL_INTERVAL_SECONDS=0.01, OUTBOX_BATCH_SIZE=100)

            # Must return normally (TimeoutError from wait_for is caught, not raised).
            await run_outbox_publisher(
                _session_factory(session), producer, settings, _NeverResolvingWaitStopEvent()
            )

            repo_instance.fetch_unpublished.assert_awaited_once()


class TestUnexpectedIterationFailures:
    async def test_cancelled_error_during_iteration_propagates(self):
        session = AsyncMock()
        with pytest.MonkeyPatch.context() as mp:
            repo_instance = AsyncMock()
            repo_instance.fetch_unpublished.side_effect = asyncio.CancelledError()
            mp.setattr("app.services.outbox.PhotoRepository", lambda: repo_instance)

            producer = AsyncMock()

            with pytest.raises(asyncio.CancelledError):
                await run_outbox_publisher(
                    _session_factory(session), producer, _settings(), _OneShotStopEvent()
                )

    async def test_unexpected_exception_during_iteration_is_logged_not_raised(self):
        """The poll loop itself must never die - an unforeseen error (e.g.
        a DB driver bug) is logged and the next poll tries again."""
        session = AsyncMock()
        with pytest.MonkeyPatch.context() as mp:
            repo_instance = AsyncMock()
            repo_instance.fetch_unpublished.side_effect = RuntimeError("unexpected DB error")
            mp.setattr("app.services.outbox.PhotoRepository", lambda: repo_instance)

            producer = AsyncMock()

            # Must not raise.
            await run_outbox_publisher(
                _session_factory(session), producer, _settings(), _OneShotStopEvent()
            )
