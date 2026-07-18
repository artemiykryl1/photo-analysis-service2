"""Kafka producer wrapper for the `photo.analysis.requested` topic.

Single `AIOKafkaProducer` instance, lazily (re)started from either
`app.main.lifespan` (best-effort, at API startup) or the outbox publisher
(`app.services.outbox`, once per poll iteration - see `ensure_started`).
The HTTP upload path never publishes directly - only the outbox publisher
calls `publish_analysis_requested` (tasks/TASK-002/20_design.md §4).
`acks="all"` + `enable_idempotence=True` per constitution.md §2.4.

Review-1 fix (tasks/TASK-002/40_review-1.md B1): if `producer.start()`
fails at API startup because Kafka is not reachable yet (e.g. the broker
is still finishing its KRaft bootstrap while `api` only waits for
`kafka: service_started`), the producer must be able to (re)start itself
later without operator intervention - otherwise every outbox iteration
would fail forever and no photo would ever reach the worker. `ensure_started`
is the idempotent, race-safe lazy-start entry point for that: safe to call
on every outbox iteration, a no-op once genuinely started, and it discards
a partially-constructed `AIOKafkaProducer` on a failed `start()` so the
next call builds a fresh one instead of retrying a producer stuck in a bad
state.
"""

import asyncio
import json
import logging
from datetime import datetime

from aiokafka import AIOKafkaProducer

from app.core.config import Settings

logger = logging.getLogger(__name__)


class KafkaEventProducer:
    """Thin async wrapper around `AIOKafkaProducer` for the analysis topic."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._topic = settings.KAFKA_TOPIC_ANALYSIS_REQUESTED
        self._producer: AIOKafkaProducer | None = None
        self._started = False
        self._start_lock = asyncio.Lock()

    def _build_producer(self) -> AIOKafkaProducer:
        return AIOKafkaProducer(
            bootstrap_servers=self._settings.KAFKA_BOOTSTRAP_SERVERS,
            acks="all",
            enable_idempotence=True,
            request_timeout_ms=int(self._settings.KAFKA_PUBLISH_TIMEOUT_SECONDS * 1000),
        )

    async def start(self) -> None:
        """Best-effort start, used once from `app.main.lifespan`. Delegates
        to `ensure_started` - kept as a separate public name only because
        callers reading `lifespan` expect a `start()`/`stop()` pair."""
        await self.ensure_started()

    async def ensure_started(self) -> None:
        """Idempotent, race-safe lazy start.

        - No-op if already started (fast path, no lock).
        - Safe to call concurrently/repeatedly (e.g. once per outbox poll
          iteration) - a lock serializes the actual `start()` attempt.
        - On failure, the half-started `AIOKafkaProducer` is discarded so
          the NEXT call builds and starts a brand new one, rather than
          repeatedly retrying a producer object that failed to connect.
        """
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            producer = self._producer or self._build_producer()
            self._producer = producer
            try:
                await producer.start()
            except Exception:
                self._producer = None
                raise
            self._started = True

    async def stop(self) -> None:
        if self._producer is not None and self._started:
            await self._producer.stop()
        self._started = False
        self._producer = None

    async def publish_analysis_requested(
        self, photo_id: str, object_key: str, created_at: datetime, trace_id: str
    ) -> None:
        """Publish `{photo_id, object_key, created_at, trace_id}` keyed by
        `photo_id` (design §3.1). Raises on failure - the caller (outbox
        publisher) is responsible for leaving the row `not_sent` and
        retrying on the next poll. The caller must have already awaited
        `ensure_started()` for this iteration.
        """
        if self._producer is None or not self._started:
            raise RuntimeError("KafkaEventProducer.ensure_started() was not awaited")
        value = json.dumps(
            {
                "photo_id": photo_id,
                "object_key": object_key,
                "created_at": created_at.isoformat(),
                "trace_id": trace_id,
            }
        ).encode("utf-8")
        await self._producer.send_and_wait(self._topic, value=value, key=photo_id.encode("utf-8"))
