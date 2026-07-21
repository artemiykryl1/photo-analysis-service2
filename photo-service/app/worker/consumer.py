"""Kafka consumer loop for the `photo.analysis.requested` topic.

tasks/TASK-002/20_design.md §5.1-§5.2, revised by
tasks/TASK-002.1/20_design.md F1. `consume_loop` owns the poll/commit
cycle; the actual claim -> gRPC -> persist sequence lives in
`app.services.analysis_processor.AnalysisProcessor` (kept separate so it
can be unit-tested without a running Kafka broker).

Offset commit timing (constitution.md §2.4 / design §5.2, §5.6, TASK-002.1
F1): `_handle_message` returns a `MessageOutcome` and `consume_loop` acts
on it explicitly:

- `MessageOutcome.COMMIT` - either the message was processed successfully
  (including a claimed-and-terminated `done`/`failed`, or a legitimate
  duplicate/redelivery skip), OR it is a confirmed poison-pill (unparsable
  JSON, missing/blank `photo_id`/`object_key`, or a `photo_id` that is not
  a valid UUID) that can never succeed no matter how many times it is
  redelivered. The offset is committed and the loop moves on.
- `MessageOutcome.RETRY` - `AnalysisProcessor.process()` raised an
  *unexpected* exception (e.g. the DB is down, a bug). The offset is
  **NOT** committed - **and, just as importantly, `consumer.seek()` moves
  the consumer's read position back to this message's own offset**. This
  second part is not optional: `aiokafka`'s `getone()` already advanced
  the consumer's in-memory position past this message the moment it
  returned it, so simply skipping the `commit()` call is not enough to
  get the SAME message redelivered - the next `getone()` would fetch the
  NEXT message instead, and a later successful `commit()` would move the
  committed offset past the failed message forever (this was the original,
  insufficient "just don't commit" fix - see tasks/TASK-002.1/00_orchestration.md
  "процессный урок"). With the `seek()`, the loop pauses briefly
  (`_ERROR_BACKOFF_SECONDS`, interruptible by `stop_event` so shutdown is
  never delayed by more than that) and then re-reads the exact same
  message on its next iteration.

  Known, accepted trade-off (DLQ is out of scope for TASK-002/TASK-002.1,
  per feature-upload/tasks.md): if the failure is deterministic for this
  specific message (not transient), the partition "gets stuck" retrying it
  forever instead of making progress on later messages. A dead-letter
  topic / reaper is future work (TASK-003).

TASK-002.1 review-1 fix (B1): a rebalance can revoke this consumer's
partition at any point while `process()` is running (it can run for up to
`WORKER_MAX_ATTEMPTS * ANALYZER_GRPC_TIMEOUT` plus the 1/2/4s retry
backoff inside `AnalysisProcessor`, which is plenty of time for a
coordinator failover or another worker replica scaling in/out). If that
happens, both `consumer.seek()` (RETRY branch) and `consumer.commit()`
(COMMIT branch) can raise (`IllegalStateError`/`AssertionError` from
`seek()`, `CommitFailedError` from `commit()`) because the partition is no
longer assigned to this consumer. `consume_loop` is never allowed to crash
on this: both calls are wrapped, the exception is logged as a `warning`
and the loop moves on to its next iteration - the message is safe either
way, because the offset was never committed, and whichever consumer the
partition rebalances to will pick up from the last committed offset (i.e.
redeliver this same message). `seek()` in particular is not just safe to
skip in this case, it is *correct* to skip: rewinding a position on a
partition this consumer no longer owns would be meaningless.

This module also increments two worker-only Prometheus counters (
`worker_messages_dropped_total{reason}` and `worker_message_retries_total`,
`app.integrations.metrics_worker`) precisely so the accepted "partition can
get stuck" trade-off above is observable: a partition stuck retrying one
message shows up as a sustained, non-zero rate of
`worker_message_retries_total` (alert on
`increase(worker_message_retries_total[5m]) > 0` staying true), and a
sudden burst of dropped poison-pills shows up broken down by `reason`
(`unparsable`/`missing_fields`/`invalid_uuid`).

Redelivery is also still a **dedup** mechanism for the ordinary case (a
hard process crash mid-message, or a rebalance). The atomic
`pending->processing` claim (`PhotoRepository.claim_for_processing`)
commits early, inside `process()` (design §5.3 step 1) - well before the
offset commit here. Its `WHERE status='pending'` predicate returns 0 rows
- and the redelivered message is skipped - for a photo that is already
`done`/`failed` (already-terminal) OR already `processing` (already
claimed by a previous delivery), so the same photo is never analyzed
twice. It does **not** resurrect a photo whose worker died AFTER the
claim commit (status already `processing`) but BEFORE the terminal
write: redelivery of that message also finds `status='processing'`,
claims 0 rows, and is skipped - the photo is left stuck in `processing`
forever. This is a known, accepted gap in TASK-002 (no reaper yet); an
automatic reaper for photos stuck in `processing` is planned for
TASK-003.

Uses `consumer.getone()` in a polling loop (not `async for message in
consumer`) so the loop can check `stop_event` between messages and exit
cleanly after the current in-flight message finishes, without picking up
a new one - see `app.worker.main` graceful shutdown (design §5.6).
"""

import asyncio
import enum
import json
import logging
import uuid

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import CommitFailedError, IllegalStateError
from aiokafka.structs import ConsumerRecord, TopicPartition

from app.core.logging import trace_id_var
from app.db.session import SessionLocal
from app.integrations.metrics_worker import (
    worker_message_retries_total,
    worker_messages_dropped_total,
)
from app.services.analysis_processor import AnalysisProcessor

logger = logging.getLogger(__name__)

_POLL_TIMEOUT_SECONDS = 1.0

# TASK-002.1 F1: anti-hot-loop pause after an unexpected (RETRY) failure,
# before the same message is re-read. Interruptible by `stop_event` (see
# `consume_loop`) so it never delays graceful shutdown by more than this.
_ERROR_BACKOFF_SECONDS = 1.0


class MessageOutcome(enum.Enum):
    """What `consume_loop` should do with the offset after `_handle_message`
    returns (TASK-002.1 F1). An enum (rather than a bare bool) reads more
    clearly at the call site and leaves room for a future third outcome
    (e.g. DLQ) without changing the meaning of the existing two."""

    COMMIT = "commit"  # processed, or a confirmed poison-pill -> advance the offset
    RETRY = "retry"  # unexpected failure -> do not commit, Kafka will redeliver


async def consume_loop(
    consumer: AIOKafkaConsumer,
    processor: AnalysisProcessor,
    stop_event: asyncio.Event,
) -> None:
    """Poll `consumer` until `stop_event` is set, handing each message to
    `processor.process()` and committing its offset only when
    `_handle_message` reports `MessageOutcome.COMMIT` (TASK-002.1 F1)."""
    while not stop_event.is_set():
        try:
            message = await asyncio.wait_for(consumer.getone(), timeout=_POLL_TIMEOUT_SECONDS)
        except TimeoutError:
            continue  # no message within the poll window - re-check stop_event

        outcome = await _handle_message(message, processor)

        if outcome is MessageOutcome.COMMIT:
            try:
                await consumer.commit()
            except CommitFailedError:
                # The partition was revoked by a rebalance before the
                # commit landed. Nothing to compensate for: the next owner
                # of the partition resumes from the last offset it has
                # committed, so this message will simply be redelivered
                # (review-1 B1) - log and keep the loop alive.
                logger.warning(
                    "commit failed (partition revoked by rebalance); "
                    "message will be redelivered",
                    extra={
                        "topic": message.topic,
                        "partition": message.partition,
                        "offset": message.offset,
                    },
                )
        else:
            worker_message_retries_total.inc()
            # Rewind the consumer's read position back to THIS message so
            # the next `getone()` re-reads it instead of skipping ahead -
            # see the module docstring for why `seek()` (not just skipping
            # `commit()`) is required for real redelivery.
            tp = TopicPartition(message.topic, message.partition)
            try:
                consumer.seek(tp, message.offset)  # aiokafka: synchronous, no await
            except (IllegalStateError, AssertionError):
                # The partition was revoked by a rebalance while `process()`
                # was running (review-1 B1). There is nothing to rewind on
                # a partition we no longer own, and `seek()` on it raises
                # instead of being a no-op. This is not an error condition
                # for us: the new owner starts from the last committed
                # offset, i.e. from this same undelivered message - log and
                # keep the loop alive.
                logger.warning(
                    "partition revoked before rewind; redelivery handled by rebalance",
                    extra={
                        "topic": message.topic,
                        "partition": message.partition,
                        "offset": message.offset,
                    },
                )
            else:
                logger.warning(
                    "offset not committed after unexpected error; message will be redelivered",
                    extra={
                        "topic": message.topic,
                        "partition": message.partition,
                        "offset": message.offset,
                    },
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_ERROR_BACKOFF_SECONDS)
            except TimeoutError:
                pass  # normal case: backoff elapsed without a shutdown signal


def _is_missing(payload: dict, key: str) -> bool:
    value = payload.get(key)
    return not isinstance(value, str) or not value


async def _handle_message(
    message: ConsumerRecord, processor: AnalysisProcessor
) -> MessageOutcome:
    try:
        payload = json.loads(message.value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # A permanently unparsable message can never succeed on retry - log
        # and move on (offset commits in consume_loop) rather than blocking
        # the partition forever on a poison-pill message.
        logger.exception("dropping unparsable Kafka message")
        worker_messages_dropped_total.labels(reason="unparsable").inc()
        return MessageOutcome.COMMIT

    # Review-2 fix (tasks/TASK-002/41_review-2.md #2): a well-formed JSON
    # payload missing/blanking `photo_id`/`object_key` is just as much a
    # poison-pill as unparsable JSON - treat it the same way (log + skip,
    # offset commits in consume_loop) instead of letting a bare `KeyError`
    # propagate as an "unhandled error" with no useful context.
    if not isinstance(payload, dict) or _is_missing(payload, "photo_id") or _is_missing(
        payload, "object_key"
    ):
        logger.warning(
            "dropping Kafka message missing required fields",
            extra={"payload": payload if isinstance(payload, dict) else str(payload)},
        )
        worker_messages_dropped_total.labels(reason="missing_fields").inc()
        return MessageOutcome.COMMIT

    # TASK-002.1 F1 (R1, orchestrator-approved): a `photo_id` that is not a
    # valid UUID can never succeed either - `AnalysisProcessor.process()`
    # would raise `ValueError` from `uuid.UUID(photo_id)` on every single
    # redelivery. Treat it as a poison-pill (skip + commit) rather than as
    # an unexpected/RETRY-able failure, which would otherwise wedge the
    # partition forever on a message that is fundamentally undeliverable.
    try:
        uuid.UUID(payload["photo_id"])
    except ValueError:
        logger.warning(
            "dropping Kafka message with a non-UUID photo_id",
            extra={"photo_id": payload["photo_id"]},
        )
        worker_messages_dropped_total.labels(reason="invalid_uuid").inc()
        return MessageOutcome.COMMIT

    token = trace_id_var.set(payload.get("trace_id") or "-")
    try:
        async with SessionLocal() as session:
            await processor.process(session, payload["photo_id"], payload["object_key"])
    except Exception:  # noqa: BLE001 - classified below, never left to crash the loop
        # Unexpected failure (e.g. DB unavailable before the claim, a bug
        # inside `process()`) - NOT a poison-pill. Log and ask
        # `consume_loop` to NOT commit so Kafka redelivers this exact
        # message (see module docstring).
        logger.exception(
            "unexpected error while processing analysis message; offset will not be committed",
            extra={"photo_id": payload.get("photo_id")},
        )
        return MessageOutcome.RETRY
    finally:
        trace_id_var.reset(token)

    return MessageOutcome.COMMIT
