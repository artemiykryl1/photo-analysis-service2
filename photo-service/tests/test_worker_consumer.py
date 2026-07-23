"""Behavioural coverage of `app.worker.consumer`.

Design §5.2 / §13.2 + review-2 fix #2 (poison-pill payload validation):
unparsable JSON and well-formed JSON missing/blanking `photo_id`/
`object_key` must both be logged and skipped without raising - a bare
`KeyError` must never propagate. A valid message must reach
`processor.process` with `trace_id_var` set from the payload, and
`consume_loop` must commit the offset only AFTER `processor.process` has
returned (at-least-once, design §5.2/§5.6).

`AIOKafkaConsumer`/`AnalysisProcessor` are always mocked - no real Kafka
broker or DB session is used (`SessionLocal` is monkeypatched to a fake
async context manager for every test that reaches `_handle_message`'s
session-open step).
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from app.core.logging import trace_id_var
from app.worker import consumer as consumer_module
from app.worker.consumer import _handle_message, _is_missing, consume_loop


def _message(value: bytes | str):
    msg = MagicMock()
    msg.value = value if isinstance(value, bytes) else value.encode("utf-8")
    return msg


def _patch_session(monkeypatch, session: AsyncMock):
    @asynccontextmanager
    async def _fake_session_local():
        yield session

    monkeypatch.setattr(consumer_module, "SessionLocal", _fake_session_local)


class _StopAfter:
    """`is_set()` returns False for exactly `iterations` calls, then True -
    lets `consume_loop` run a known, small number of iterations instead of
    looping forever."""

    def __init__(self, iterations: int = 1):
        self._calls = 0
        self._iterations = iterations

    def is_set(self) -> bool:
        self._calls += 1
        return self._calls > self._iterations


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
        message = _message(
            json.dumps(
                {
                    "photo_id": "photo-1",
                    "object_key": "photos/photo-1/original.jpg",
                    "trace_id": "trace-xyz",
                }
            )
        )

        await _handle_message(message, processor)

        processor.process.assert_awaited_once_with(
            session, "photo-1", "photos/photo-1/original.jpg"
        )

    async def test_valid_message_sets_trace_id_var_during_processing(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        observed = {}

        async def _capture_process(_session, _photo_id, _object_key):
            observed["trace_id"] = trace_id_var.get()

        processor = AsyncMock()
        processor.process.side_effect = _capture_process
        message = _message(json.dumps({"photo_id": "photo-1", "object_key": "key", "trace_id": "trace-xyz"}))

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
        message = _message(json.dumps({"photo_id": "photo-1", "object_key": "key"}))

        await _handle_message(message, processor)

        assert observed["trace_id"] == "-"

    async def test_unhandled_exception_from_processor_is_logged_not_raised(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        processor = AsyncMock()
        processor.process.side_effect = RuntimeError("db exploded")
        message = _message(json.dumps({"photo_id": "photo-1", "object_key": "key"}))

        await _handle_message(message, processor)  # must not raise


class TestConsumeLoop:
    async def test_processes_one_message_then_commits_offset(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)

        consumer = AsyncMock()
        message = _message(json.dumps({"photo_id": "p1", "object_key": "k"}))
        consumer.getone.return_value = message
        processor = AsyncMock()

        await consume_loop(consumer, processor, _StopAfter(iterations=1))

        processor.process.assert_awaited_once_with(session, "p1", "k")
        consumer.commit.assert_awaited_once()

    async def test_commit_happens_after_process_completes(self, monkeypatch):
        session = AsyncMock()
        _patch_session(monkeypatch, session)
        order: list[str] = []

        consumer = AsyncMock()
        message = _message(json.dumps({"photo_id": "p1", "object_key": "k"}))
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
        consumer.getone.side_effect = [
            _message(json.dumps({"photo_id": "p1", "object_key": "k1"})),
            _message(json.dumps({"photo_id": "p2", "object_key": "k2"})),
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
