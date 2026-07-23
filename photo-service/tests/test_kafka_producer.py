"""Behavioural coverage of `app.integrations.kafka_producer.KafkaEventProducer`.

`AIOKafkaProducer` itself is always mocked - no real Kafka broker connection
is made. Focus: the review-1 `ensure_started` self-healing contract (BLK-1,
tasks/TASK-002/40_review-1.md) - idempotent, race-safe lazy start, and a
failed `start()` discards the half-built producer so the next call builds a
fresh one instead of retrying a stuck object.
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import Settings
from app.integrations.kafka_producer import KafkaEventProducer


def _settings() -> Settings:
    return Settings(KAFKA_TOPIC_ANALYSIS_REQUESTED="photo.analysis.requested")


class TestEnsureStarted:
    async def test_first_call_builds_and_starts_producer(self):
        with patch("app.integrations.kafka_producer.AIOKafkaProducer") as mock_cls:
            fake_producer = AsyncMock()
            mock_cls.return_value = fake_producer

            producer = KafkaEventProducer(_settings())
            await producer.ensure_started()

            mock_cls.assert_called_once()
            fake_producer.start.assert_awaited_once()
            assert producer._started is True

    async def test_second_call_is_a_noop_fast_path(self):
        with patch("app.integrations.kafka_producer.AIOKafkaProducer") as mock_cls:
            fake_producer = AsyncMock()
            mock_cls.return_value = fake_producer

            producer = KafkaEventProducer(_settings())
            await producer.ensure_started()
            await producer.ensure_started()

            mock_cls.assert_called_once()  # not rebuilt
            fake_producer.start.assert_awaited_once()  # not restarted

    async def test_failed_start_discards_producer_and_reraises(self):
        with patch("app.integrations.kafka_producer.AIOKafkaProducer") as mock_cls:
            fake_producer = AsyncMock()
            fake_producer.start.side_effect = ConnectionError("kafka unreachable")
            mock_cls.return_value = fake_producer

            producer = KafkaEventProducer(_settings())

            with pytest.raises(ConnectionError):
                await producer.ensure_started()

            assert producer._started is False
            assert producer._producer is None

    async def test_next_call_after_failure_builds_a_fresh_producer(self):
        with patch("app.integrations.kafka_producer.AIOKafkaProducer") as mock_cls:
            failing_producer = AsyncMock()
            failing_producer.start.side_effect = ConnectionError("down")
            healthy_producer = AsyncMock()
            mock_cls.side_effect = [failing_producer, healthy_producer]

            producer = KafkaEventProducer(_settings())

            with pytest.raises(ConnectionError):
                await producer.ensure_started()

            await producer.ensure_started()  # self-heals on next call

            assert mock_cls.call_count == 2
            healthy_producer.start.assert_awaited_once()
            assert producer._started is True

    async def test_concurrent_ensure_started_calls_only_start_the_producer_once(self):
        """Race-safety (review-1 BLK-1): two `ensure_started()` callers
        racing (e.g. `app.main.lifespan` startup vs. the first outbox poll
        iteration) must serialize on the lock - the second one, having
        already passed the outer `if self._started` check before the
        first finished, must observe `_started=True` once it acquires the
        lock and return without building/starting a second producer."""
        with patch("app.integrations.kafka_producer.AIOKafkaProducer") as mock_cls:
            fake_producer = AsyncMock()

            async def _slow_start():
                await asyncio.sleep(0.01)

            fake_producer.start.side_effect = _slow_start
            mock_cls.return_value = fake_producer

            producer = KafkaEventProducer(_settings())

            await asyncio.gather(producer.ensure_started(), producer.ensure_started())

            mock_cls.assert_called_once()
            fake_producer.start.assert_awaited_once()
            assert producer._started is True

    async def test_start_delegates_to_ensure_started(self):
        with patch("app.integrations.kafka_producer.AIOKafkaProducer") as mock_cls:
            fake_producer = AsyncMock()
            mock_cls.return_value = fake_producer

            producer = KafkaEventProducer(_settings())
            await producer.start()

            fake_producer.start.assert_awaited_once()
            assert producer._started is True


class TestStop:
    async def test_stop_when_never_started_is_a_noop(self):
        producer = KafkaEventProducer(_settings())
        await producer.stop()  # must not raise
        assert producer._started is False

    async def test_stop_after_started_stops_underlying_producer(self):
        with patch("app.integrations.kafka_producer.AIOKafkaProducer") as mock_cls:
            fake_producer = AsyncMock()
            mock_cls.return_value = fake_producer

            producer = KafkaEventProducer(_settings())
            await producer.ensure_started()
            await producer.stop()

            fake_producer.stop.assert_awaited_once()
            assert producer._started is False
            assert producer._producer is None


class TestPublishAnalysisRequested:
    async def test_raises_runtime_error_if_ensure_started_never_awaited(self):
        producer = KafkaEventProducer(_settings())

        with pytest.raises(RuntimeError):
            await producer.publish_analysis_requested(
                "photo-1", "photos/photo-1/original.jpg", datetime.now(timezone.utc), "trace-1"
            )

    async def test_publishes_expected_json_payload_keyed_by_photo_id(self):
        with patch("app.integrations.kafka_producer.AIOKafkaProducer") as mock_cls:
            fake_producer = AsyncMock()
            mock_cls.return_value = fake_producer

            producer = KafkaEventProducer(_settings())
            await producer.ensure_started()

            created_at = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
            await producer.publish_analysis_requested(
                "photo-1", "photos/photo-1/original.jpg", created_at, "trace-abc"
            )

            fake_producer.send_and_wait.assert_awaited_once()
            call = fake_producer.send_and_wait.call_args
            assert call.args[0] == "photo.analysis.requested"
            assert call.kwargs["key"] == b"photo-1"
            payload = json.loads(call.kwargs["value"].decode("utf-8"))
            assert payload == {
                "photo_id": "photo-1",
                "object_key": "photos/photo-1/original.jpg",
                "created_at": created_at.isoformat(),
                "trace_id": "trace-abc",
            }

    async def test_send_failure_propagates_to_caller(self):
        with patch("app.integrations.kafka_producer.AIOKafkaProducer") as mock_cls:
            fake_producer = AsyncMock()
            fake_producer.send_and_wait.side_effect = TimeoutError("broker slow")
            mock_cls.return_value = fake_producer

            producer = KafkaEventProducer(_settings())
            await producer.ensure_started()

            with pytest.raises(TimeoutError):
                await producer.publish_analysis_requested(
                    "photo-1", "key", datetime.now(timezone.utc), "trace-1"
                )
