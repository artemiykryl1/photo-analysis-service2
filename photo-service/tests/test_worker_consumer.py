"""Behavioural coverage of `app.worker.consumer`.

Design §5.2 / §13.2 + review-2 fix #2 (poison-pill payload validation):
unparsable JSON and well-formed JSON missing/blanking `photo_id`/
`object_key` must both be logged and skipped without raising - a bare
`KeyError` must never propagate. A valid message must reach
`processor.process` with `trace_id_var` set from the payload, and
`consume_loop` must commit the offset only AFTER `processor.process` has
returned (at-least-once, design §5.2/§5.6).

TASK-002.1 (tasks/TASK-002.1/20_design.md F1, review-1 B1): additional
classes below cover the new `MessageOutcome.RETRY` branch (unexpected
exception -> offset NOT committed, `consumer.seek()` rewinds to the
message's own offset, `worker_message_retries_total` increments, the
anti-hot-loop pause is interruptible by `stop_event`), the rebalance
guard (`seek()`/`commit()` raising must never crash `consume_loop`), the
new `photo_id`-not-a-UUID poison-pill branch, and the
`worker_messages_dropped_total{reason}` increments for all three
poison-pill reasons.

`AIOKafkaConsumer`/`AnalysisProcessor` are always mocked - no real Kafka
broker or DB session is used (`SessionLocal` is monkeypatched to a fake
async context manager for every test that reaches `_handle_message`'s
session-open step). `consumer.seek` is always set to a plain `MagicMock`
(not left as the `AsyncMock` auto-attribute default) because `seek()` is
synchronous in `aiokafka` - leaving it as an `AsyncMock` would silently
create an unawaited coroutine on every call instead of actually invoking
a mock synchronously.
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from aiokafka.errors import CommitFailedError, IllegalStateError
from aiokafka.structs import TopicPartition

from app.core.logging import trace_id_var
from app.integrations.metrics_worker import (
    worker_message_retries_total,
    worker_messages_dropped_total,
)
from app.worker import consumer as consumer_module
from app.worker.consumer import MessageOutcome, _handle_message, _is_missing, consume_loop


def _message(
    value: bytes | str,
    *,
    topic: str = "photo.analysis.requested",
    partition: int = 0,
    offset: int = 0,
):
    msg = MagicMock()
    msg.value = value if isinstance(value, bytes) else value.encode("utf-8")
    msg.topic = topic
    msg.partition = partition
    msg.offset = offset
    return msg


def _counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get() if labels else counter._value.get()


def _patch_session(monkeypatch, session: AsyncMock):
    @asynccontextmanager
    async def _fake_session_local():
        yield session

    monkeypatch.setattr(consumer_module, "SessionLocal", _fake_session_local)


class _StopAfter:
    """`is_set()` returns False for exactly `iterations` calls, then True -
    lets `consume_loop` run a known, small number of iterations instead of
    looping forever.

    Also implements an always-immediately-resolving `.wait()` - needed
    because the F1 RETRY-path anti-hot-loop pause awaits
    `stop_event.wait()`. Resolving instantly means RETRY-path tests never
    actually wait out the real `_ERROR_BACKOFF_SECONDS`, regardless of its
    value, without having to monkeypatch it in every such test. Tests that
    specifically want to prove the pause IS interruptible mid-wait (rather
    than just not blocking) use a real `asyncio.Event` instead - see
    `TestConsumeLoopRetryBackoffInterruptible`.
    """

    def __init__(self, iterations: int = 1):
        self._calls = 0
        self._iterations = iterations

    def is_set(self) -> bool:
        self._calls += 1
        return self._calls > self._iterations

    async def wait(self) -> None:
        return


class TestIsMissing:
    def test_missing_key_is_missing(self):
        assert _is_missing({}, "photo_id") is True

    def test_empty_string_is_missing(self):
        assert _is_missing({"photo_id": ""}, "photo_id") is True

    def test_non_string_value_is_missing(self):
        assert _is_missing({"photo_id": 123}, "photo_id") is True

    def test_non_empty_string_is_present(self):
        assert _is_missing({"photo_id": "abc"}, "photo_id") is False


class TestHandleMessagePoisonPill:
    async def test_unparsable_json_is_dropped_without_raising(self):
        processor = AsyncMock()
        message = _message(b"{not valid json")

        await _handle_message(message, processor)  # must not raise

        processor.process.assert_not_called()

    async def test_non_utf8_bytes_are_dropped_without_raising(self):
        processor = AsyncMock()
        message = _message(b"\xff\xfe\x00\x01")

        await _handle_message(message, processor)

        processor.process.assert_not_called()

    async def test_json_array_instead_of_object_is_dropped(self):
        processor = AsyncMock()
        message = _message(json.dumps(["photo_id", "object_key"]))

        await _handle_message(message, processor)

        processor.process.assert_not_called()


class TestHandleMessageMissingFields:
    async def test_missing_photo_id_is_dropped(self):
        processor = AsyncMock()
        message = _message(json.dumps({"object_key": "photos/x/original.jpg"}))

        await _handle_message(message, processor)

        processor.process.assert_not_called()

    async def test_missing_object_key_is_dropped(self):
        processor = AsyncMock()
        message = _message(json.dumps({"photo_id": "abc-123"}))

        await _handle_message(message, processor)

        processor.process.assert_not_called()

    async def test_empty_string_photo_id_is_dropped(self):
        processor = AsyncMock()
        message = _message(json.dumps({"photo_id": "", "object_key": "k"}))

        await _handle_message(message, processor)

        processor.process.assert_not_called()

    async def test_does_not_raise_keyerror_on_bare_missing_fields(self):
        """Review-2 fix #2: this used to be a bare `payload["photo_id"]`
        KeyError before the guard was added."""
        processor = AsyncMock()
        message = _message(json.dumps({"trace_id": "only-trace-id"}))

        await _handle_message(message, processor)  # must not raise KeyError


class TestHandleMessageValid:
    async def test_valid_message_calls_processor_process_with_session(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        processor = AsyncMock()
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(
            json.dumps(
                {
                    "photo_id": photo_id,
                    "object_key": f"photos/{photo_id}/original.jpg",
                    "trace_id": "trace-xyz",
                }
            )
        )

        await _handle_message(message, processor)

        processor.process.assert_awaited_once_with(
            session, photo_id, f"photos/{photo_id}/original.jpg"
        )

    async def test_valid_message_sets_trace_id_var_during_processing(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        observed = {}

        async def _capture_process(_session, _photo_id, _object_key):
            observed["trace_id"] = trace_id_var.get()

        processor = AsyncMock()
        processor.process.side_effect = _capture_process
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(
            json.dumps({"photo_id": photo_id, "object_key": "key", "trace_id": "trace-xyz"})
        )

        await _handle_message(message, processor)

        assert observed["trace_id"] == "trace-xyz"
        assert trace_id_var.get() == "-"  # reset after handling

    async def test_missing_trace_id_falls_back_to_dash(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        observed = {}

        async def _capture_process(_session, _photo_id, _object_key):
            observed["trace_id"] = trace_id_var.get()

        processor = AsyncMock()
        processor.process.side_effect = _capture_process
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(json.dumps({"photo_id": photo_id, "object_key": "key"}))

        await _handle_message(message, processor)

        assert observed["trace_id"] == "-"

    async def test_unhandled_exception_from_processor_is_logged_not_raised(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        processor = AsyncMock()
        processor.process.side_effect = RuntimeError("db exploded")
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(json.dumps({"photo_id": photo_id, "object_key": "key"}))

        await _handle_message(message, processor)  # must not raise


class TestConsumeLoop:
    async def test_processes_one_message_then_commits_offset(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)

        consumer = AsyncMock()
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(json.dumps({"photo_id": photo_id, "object_key": "k"}))
        consumer.getone.return_value = message
        processor = AsyncMock()

        await consume_loop(consumer, processor, _StopAfter(iterations=1))

        processor.process.assert_awaited_once_with(session, photo_id, "k")
        consumer.commit.assert_awaited_once()

    async def test_commit_happens_after_process_completes(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        order: list[str] = []

        consumer = AsyncMock()
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(json.dumps({"photo_id": photo_id, "object_key": "k"}))
        consumer.getone.return_value = message

        async def _fake_commit():
            order.append("commit")

        consumer.commit.side_effect = _fake_commit

        processor = AsyncMock()

        async def _fake_process(*_args, **_kwargs):
            order.append("process")

        processor.process.side_effect = _fake_process

        await consume_loop(consumer, processor, _StopAfter(iterations=1))

        assert order == ["process", "commit"]

    async def test_multiple_messages_each_get_their_own_commit(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)

        consumer = AsyncMock()
        photo_id_1 = "11111111-1111-1111-1111-111111111111"
        photo_id_2 = "22222222-2222-2222-2222-222222222222"
        consumer.getone.side_effect = [
            _message(json.dumps({"photo_id": photo_id_1, "object_key": "k1"})),
            _message(json.dumps({"photo_id": photo_id_2, "object_key": "k2"})),
        ]
        processor = AsyncMock()

        await consume_loop(consumer, processor, _StopAfter(iterations=2))

        assert processor.process.await_count == 2
        assert consumer.commit.await_count == 2

    async def test_stop_event_already_set_never_calls_getone(self):
        consumer = AsyncMock()
        processor = AsyncMock()
        stop_event = MagicMock()
        stop_event.is_set.return_value = True

        await consume_loop(consumer, processor, stop_event)

        consumer.getone.assert_not_called()
        consumer.commit.assert_not_called()

    async def test_poison_pill_message_still_commits_and_does_not_kill_loop(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)

        consumer = AsyncMock()
        consumer.getone.return_value = _message(b"{not valid json")
        processor = AsyncMock()

        await consume_loop(consumer, processor, _StopAfter(iterations=1))

        processor.process.assert_not_called()
        consumer.commit.assert_awaited_once()  # offset still advances past the poison pill

    async def test_poll_timeout_with_no_message_rechecks_stop_event_without_committing(
        self, monkeypatch
    ):
        """When no message arrives within `_POLL_TIMEOUT_SECONDS`,
        `consumer.getone()` never resolves and the loop must re-check
        `stop_event` instead of committing or calling the processor."""
        monkeypatch.setattr(consumer_module, "_POLL_TIMEOUT_SECONDS", 0.01)

        consumer = AsyncMock()

        async def _never_resolves():
            import asyncio

            await asyncio.Event().wait()

        consumer.getone.side_effect = _never_resolves
        processor = AsyncMock()

        await consume_loop(consumer, processor, _StopAfter(iterations=1))

        processor.process.assert_not_called()
        consumer.commit.assert_not_called()


class TestHandleMessageInvalidUuid:
    """TASK-002.1 F1 (R1): a `photo_id` that is not a valid UUID can never
    succeed on any redelivery - it is treated as a poison-pill (COMMIT),
    exactly like unparsable JSON or missing fields."""

    async def test_non_uuid_photo_id_is_a_confirmed_poison_pill(self):
        processor = AsyncMock()
        message = _message(json.dumps({"photo_id": "not-a-uuid", "object_key": "k"}))

        outcome = await _handle_message(message, processor)

        assert outcome is MessageOutcome.COMMIT
        processor.process.assert_not_called()

    async def test_non_uuid_photo_id_via_consume_loop_still_commits(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        consumer = AsyncMock()
        consumer.seek = MagicMock()
        consumer.getone.return_value = _message(
            json.dumps({"photo_id": "abc-xyz", "object_key": "k"})
        )
        processor = AsyncMock()

        await consume_loop(consumer, processor, _StopAfter(iterations=1))

        processor.process.assert_not_called()
        consumer.commit.assert_awaited_once()
        consumer.seek.assert_not_called()


class TestDroppedMessageMetrics:
    """review-1 M1: every poison-pill reason must be independently
    observable via `worker_messages_dropped_total{reason}` - a
    `logger.warning`/`logger.exception` call alone is not alertable."""

    async def test_unparsable_json_increments_dropped_unparsable_reason(self):
        processor = AsyncMock()
        message = _message(b"{not valid json")
        before = _counter_value(worker_messages_dropped_total, reason="unparsable")

        await _handle_message(message, processor)

        after = _counter_value(worker_messages_dropped_total, reason="unparsable")
        assert after == before + 1

    async def test_missing_fields_increments_dropped_missing_fields_reason(self):
        processor = AsyncMock()
        message = _message(json.dumps({"trace_id": "only-trace-id"}))
        before = _counter_value(worker_messages_dropped_total, reason="missing_fields")

        await _handle_message(message, processor)

        after = _counter_value(worker_messages_dropped_total, reason="missing_fields")
        assert after == before + 1

    async def test_invalid_uuid_increments_dropped_invalid_uuid_reason(self):
        processor = AsyncMock()
        message = _message(json.dumps({"photo_id": "not-a-uuid", "object_key": "k"}))
        before = _counter_value(worker_messages_dropped_total, reason="invalid_uuid")

        await _handle_message(message, processor)

        after = _counter_value(worker_messages_dropped_total, reason="invalid_uuid")
        assert after == before + 1


class TestConsumeLoopHappyPathDoesNotSeek:
    async def test_successful_processing_never_calls_seek(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        consumer = AsyncMock()
        consumer.seek = MagicMock()
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(json.dumps({"photo_id": photo_id, "object_key": "k"}))
        consumer.getone.return_value = message
        processor = AsyncMock()

        await consume_loop(consumer, processor, _StopAfter(iterations=1))

        consumer.seek.assert_not_called()
        consumer.commit.assert_awaited_once()


class TestConsumeLoopRetryPath:
    """TASK-002.1 F1 (main critical scenario): `process()` raising an
    unexpected exception must NOT commit the offset, and must rewind the
    consumer's read position back to the failed message's own
    (topic, partition, offset) so it is genuinely redelivered - see the
    module docstring for why `seek()` (not just skipping `commit()`) is
    required."""

    async def test_unexpected_exception_does_not_commit_the_offset(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        consumer = AsyncMock()
        consumer.seek = MagicMock()
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(json.dumps({"photo_id": photo_id, "object_key": "k"}))
        consumer.getone.return_value = message
        processor = AsyncMock()
        processor.process.side_effect = RuntimeError("db exploded")

        await consume_loop(consumer, processor, _StopAfter(iterations=1))

        consumer.commit.assert_not_called()

    async def test_unexpected_exception_seeks_to_this_messages_own_topic_partition_offset(
        self, monkeypatch
    ):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        consumer = AsyncMock()
        consumer.seek = MagicMock()
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(
            json.dumps({"photo_id": photo_id, "object_key": "k"}),
            topic="photo.analysis.requested",
            partition=3,
            offset=4242,
        )
        consumer.getone.return_value = message
        processor = AsyncMock()
        processor.process.side_effect = RuntimeError("db exploded")

        await consume_loop(consumer, processor, _StopAfter(iterations=1))

        consumer.seek.assert_called_once_with(
            TopicPartition("photo.analysis.requested", 3), 4242
        )

    async def test_worker_message_retries_total_increments_on_retry(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        consumer = AsyncMock()
        consumer.seek = MagicMock()
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(json.dumps({"photo_id": photo_id, "object_key": "k"}))
        consumer.getone.return_value = message
        processor = AsyncMock()
        processor.process.side_effect = RuntimeError("boom")

        before = _counter_value(worker_message_retries_total)
        await consume_loop(consumer, processor, _StopAfter(iterations=1))
        after = _counter_value(worker_message_retries_total)

        assert after == before + 1

    async def test_next_iteration_after_retry_still_works_loop_is_alive(self, monkeypatch):
        """The RETRY branch must not leave `consume_loop` unable to make
        progress: a second message (on a second iteration) is still
        handled normally."""
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        consumer = AsyncMock()
        consumer.seek = MagicMock()
        photo_id_1 = "11111111-1111-1111-1111-111111111111"
        photo_id_2 = "22222222-2222-2222-2222-222222222222"
        consumer.getone.side_effect = [
            _message(json.dumps({"photo_id": photo_id_1, "object_key": "k1"})),
            _message(json.dumps({"photo_id": photo_id_2, "object_key": "k2"})),
        ]
        processor = AsyncMock()
        processor.process.side_effect = [RuntimeError("boom"), None]

        await consume_loop(consumer, processor, _StopAfter(iterations=2))

        assert processor.process.await_count == 2
        consumer.commit.assert_awaited_once()  # only the 2nd (successful) message commits


class TestConsumeLoopRetryBackoffInterruptible:
    async def test_pause_is_interrupted_by_stop_event_not_waited_out_in_full(self, monkeypatch):
        """Graceful shutdown (F1 design): the anti-hot-loop pause after an
        unexpected error must not delay shutdown by the full backoff -
        `stop_event.wait()` wakes it as soon as shutdown is signalled."""
        monkeypatch.setattr(consumer_module, "_ERROR_BACKOFF_SECONDS", 5.0)
        session = AsyncMock()
        _patch_session(monkeypatch, session)

        consumer = AsyncMock()
        consumer.seek = MagicMock()
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(json.dumps({"photo_id": photo_id, "object_key": "k"}))
        consumer.getone.return_value = message
        processor = AsyncMock()
        processor.process.side_effect = RuntimeError("boom")

        stop_event = asyncio.Event()

        async def _signal_shutdown_shortly_after_start():
            await asyncio.sleep(0.05)
            stop_event.set()

        setter = asyncio.ensure_future(_signal_shutdown_shortly_after_start())
        start = time.monotonic()
        await consume_loop(consumer, processor, stop_event)
        elapsed = time.monotonic() - start
        await setter

        assert elapsed < 1.0  # much less than the 5s backoff - pause was interrupted


class TestConsumeLoopRebalanceGuard:
    """review-1 B1 (BLOCKING, closed): a rebalance can revoke this
    consumer's partition while `process()` is running - both `seek()`
    (RETRY branch) and `commit()` (COMMIT branch) can then raise. Neither
    call is allowed to crash `consume_loop`."""

    async def test_seek_illegal_state_error_does_not_crash_the_loop(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        consumer = AsyncMock()
        consumer.seek = MagicMock(side_effect=IllegalStateError("no current assignment"))
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(json.dumps({"photo_id": photo_id, "object_key": "k"}))
        consumer.getone.return_value = message
        processor = AsyncMock()
        processor.process.side_effect = RuntimeError("boom")

        await consume_loop(consumer, processor, _StopAfter(iterations=1))  # must not raise

        consumer.commit.assert_not_called()
        consumer.seek.assert_called_once()

    async def test_seek_assertion_error_does_not_crash_the_loop(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        consumer = AsyncMock()
        consumer.seek = MagicMock(side_effect=AssertionError("no assignment at all"))
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(json.dumps({"photo_id": photo_id, "object_key": "k"}))
        consumer.getone.return_value = message
        processor = AsyncMock()
        processor.process.side_effect = RuntimeError("boom")

        await consume_loop(consumer, processor, _StopAfter(iterations=1))  # must not raise

        consumer.commit.assert_not_called()

    async def test_commit_failed_error_does_not_crash_the_loop_and_does_not_seek(
        self, monkeypatch
    ):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        consumer = AsyncMock()
        consumer.seek = MagicMock()
        consumer.commit = AsyncMock(side_effect=CommitFailedError("rebalance in progress"))
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(json.dumps({"photo_id": photo_id, "object_key": "k"}))
        consumer.getone.return_value = message
        processor = AsyncMock()  # success path - reaches the COMMIT branch

        await consume_loop(consumer, processor, _StopAfter(iterations=1))  # must not raise

        consumer.seek.assert_not_called()

    async def test_seek_failure_still_leaves_the_loop_able_to_process_a_later_message(
        self, monkeypatch
    ):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        consumer = AsyncMock()
        consumer.seek = MagicMock(side_effect=IllegalStateError("no current assignment"))
        photo_id_1 = "11111111-1111-1111-1111-111111111111"
        photo_id_2 = "22222222-2222-2222-2222-222222222222"
        consumer.getone.side_effect = [
            _message(json.dumps({"photo_id": photo_id_1, "object_key": "k1"})),
            _message(json.dumps({"photo_id": photo_id_2, "object_key": "k2"})),
        ]
        processor = AsyncMock()
        processor.process.side_effect = [RuntimeError("boom"), None]

        await consume_loop(consumer, processor, _StopAfter(iterations=2))

        assert processor.process.await_count == 2
        consumer.commit.assert_awaited_once()


class TestConsumeLoopRetryBackoffElapsesNormally:
    async def test_backoff_times_out_without_a_shutdown_signal(self, monkeypatch):
        """The ordinary (non-shutdown) case: `stop_event` is never set
        during the pause, so `asyncio.wait_for` genuinely times out after
        `_ERROR_BACKOFF_SECONDS` and the loop simply moves on to its next
        iteration (`except TimeoutError: pass`)."""
        monkeypatch.setattr(consumer_module, "_ERROR_BACKOFF_SECONDS", 0.01)
        session = AsyncMock()
        _patch_session(monkeypatch, session)

        consumer = AsyncMock()
        consumer.seek = MagicMock()
        photo_id = "11111111-1111-1111-1111-111111111111"
        message = _message(json.dumps({"photo_id": photo_id, "object_key": "k"}))
        consumer.getone.return_value = message
        processor = AsyncMock()
        processor.process.side_effect = RuntimeError("boom")

        class _NeverSignalledStopEvent(_StopAfter):
            """`.wait()` blocks forever (backed by a fresh, never-`set()`
            `asyncio.Event`) - the pause can only end via the
            `_ERROR_BACKOFF_SECONDS` timeout, never via an interrupt."""

            async def wait(self) -> None:
                await asyncio.Event().wait()

        await consume_loop(consumer, processor, _NeverSignalledStopEvent(iterations=1))

        consumer.commit.assert_not_called()
        consumer.seek.assert_called_once()
