"""Kafka consumer loop for the `photo.analysis.requested` topic.

tasks/TASK-002/20_design.md §5.1-§5.2. `consume_loop` owns the poll/commit
cycle; the actual claim -> gRPC -> persist sequence lives in
`app.services.analysis_processor.AnalysisProcessor` (kept separate so it
can be unit-tested without a running Kafka broker).

Offset commit timing (constitution.md §2.4 / design §5.2, §5.6): a message
is only ever committed AFTER `AnalysisProcessor.process()` has returned -
whether it reached a terminal `done`/`failed` write or (rare) raised an
unexpected exception. This is deliberately at-least-once: a hard process
crash mid-message leaves the offset uncommitted and Kafka will redeliver
it (the atomic `pending->processing` claim makes redelivery of an
already-terminal photo a safe no-op - see `PhotoRepository.claim_for_processing`).

Uses `consumer.getone()` in a polling loop (not `async for message in
consumer`) so the loop can check `stop_event` between messages and exit
cleanly after the current in-flight message finishes, without picking up
a new one - see `app.worker.main` graceful shutdown (design §5.6).
"""

import asyncio
import json
import logging

from aiokafka import AIOKafkaConsumer
from aiokafka.structs import ConsumerRecord

from app.core.logging import trace_id_var
from app.db.session import SessionLocal
from app.services.analysis_processor import AnalysisProcessor

logger = logging.getLogger(__name__)

_POLL_TIMEOUT_SECONDS = 1.0


async def consume_loop(
    consumer: AIOKafkaConsumer,
    processor: AnalysisProcessor,
    stop_event: asyncio.Event,
) -> None:
    """Poll `consumer` until `stop_event` is set, handing each message to
    `processor.process()` and committing its offset afterwards."""
    while not stop_event.is_set():
        try:
            message = await asyncio.wait_for(consumer.getone(), timeout=_POLL_TIMEOUT_SECONDS)
        except TimeoutError:
            continue  # no message within the poll window - re-check stop_event

        await _handle_message(message, processor)
        await consumer.commit()


def _is_missing(payload: dict, key: str) -> bool:
    value = payload.get(key)
    return not isinstance(value, str) or not value


async def _handle_message(message: ConsumerRecord, processor: AnalysisProcessor) -> None:
    try:
        payload = json.loads(message.value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # A permanently unparsable message can never succeed on retry - log
        # and move on (offset still commits in consume_loop) rather than
        # blocking the partition forever on a poison-pill message.
        logger.exception("dropping unparsable Kafka message")
        return

    # Review-2 fix (tasks/TASK-002/41_review-2.md #2): a well-formed JSON
    # payload missing/blanking `photo_id`/`object_key` is just as much a
    # poison-pill as unparsable JSON - treat it the same way (log + skip,
    # offset still commits in consume_loop) instead of letting a bare
    # `KeyError` propagate as an "unhandled error" with no useful context.
    if not isinstance(payload, dict) or _is_missing(payload, "photo_id") or _is_missing(
        payload, "object_key"
    ):
        logger.warning(
            "dropping Kafka message missing required fields",
            extra={"payload": payload if isinstance(payload, dict) else str(payload)},
        )
        return

    token = trace_id_var.set(payload.get("trace_id") or "-")
    try:
        async with SessionLocal() as session:
            await processor.process(session, payload["photo_id"], payload["object_key"])
    except Exception:  # noqa: BLE001 - never let one bad message kill the consumer loop
        logger.exception(
            "unhandled error while processing analysis message",
            extra={"photo_id": payload.get("photo_id")},
        )
    finally:
        trace_id_var.reset(token)
