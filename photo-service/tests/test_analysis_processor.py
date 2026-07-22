"""Behavioural coverage of `app.services.analysis_processor.AnalysisProcessor`.

Design §5.3 / §12 step 11, extended by TASK-003 A8
(tasks/TASK-003/20_design.md §5.2): claim -> download+prepare image (once,
cached across retries) -> bounded gRPC retry loop -> terminal write.
`PhotoRepository`, `AnalysisResultRepository`, `ObjectStorage` and the gRPC
`AnalyzerGrpcClient` are always mocked here - no real DB/Kafka/gRPC/MinIO
connection is made (the real analyzer is never called, per the
test-writer brief). `asyncio.sleep` between retries (and the new
UNAVAILABLE/DEADLINE_EXCEEDED cooldown) is monkeypatched to a no-op so
retry/exhaustion tests run instantly. `prepare_for_analysis` is
monkeypatched to a fast, deterministic identity-shaped stand-in - the real
Pillow-based downscaling ladder has its own dedicated tests
(`test_image_prep.py`, test-writer); here we only need SOME
`PreparedImage` to flow through the gRPC call and metrics.

TASK-002.1 (tasks/TASK-002.1/20_design.md F3): `AnalysisProcessor` now
calls `_maybe_complete_batch` after every terminal write (skip/done/failed).
Every existing test below sets `photos.get_batch_id.return_value = None`
so `_maybe_complete_batch` deterministically no-ops on its first check
(single-photo path, no batch) instead of relying on `AsyncMock`'s default
auto-mocked return value happening to short-circuit later - the
`session.commit` await-count assertions in `TestMetricsAndCommitOrdering`
depend on this. Dedicated coverage of batch-completion itself
(`_maybe_complete_batch` with a real batch_id) is added separately
(test-writer).
"""

import time
import uuid
from datetime import datetime, timezone
from unittest.mock import ANY, AsyncMock, MagicMock

import grpc
import pytest

from app.core.config import Settings
from app.db.models import AnalysisResult, Photo, PhotoStatus
from app.integrations.metrics_worker import worker_messages_processed_total
from app.services import analysis_processor as analysis_processor_module
from app.services.analysis_processor import AnalysisProcessor
from app.services.image_prep import PreparedImage

_FAKE_RAW_BYTES = b"fake-raw-image-bytes"
_FAKE_PREPARED_BYTES = b"fake-prepared-image-bytes"


def _counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get() if labels else counter._value.get()


def _rpc_error(code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(
        code=code,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details=f"{code.name}",
    )


def _settings(**overrides) -> Settings:
    return Settings(
        WORKER_MAX_ATTEMPTS=overrides.get("WORKER_MAX_ATTEMPTS", 3),
        RETRY_BACKOFF_BASE_SECONDS=overrides.get("RETRY_BACKOFF_BASE_SECONDS", 1.0),
        ANALYZER_GRPC_TIMEOUT=overrides.get("ANALYZER_GRPC_TIMEOUT", 5.0),
        ANALYZER_UNAVAILABLE_COOLDOWN_SECONDS=overrides.get(
            "ANALYZER_UNAVAILABLE_COOLDOWN_SECONDS", 5.0
        ),
    )


def _fake_storage(data: bytes = _FAKE_RAW_BYTES) -> MagicMock:
    """A `MagicMock` (not `AsyncMock`) - `ObjectStorage.get_file` is a
    synchronous method invoked via `anyio.to_thread.run_sync`, so it must
    return plain bytes directly, not a coroutine."""
    storage = MagicMock()
    storage.get_file.return_value = data
    return storage


def _make_processor(
    photos=None,
    analysis=None,
    analyzer=None,
    settings=None,
    batch_repository=None,
    storage=None,
) -> AnalysisProcessor:
    """TASK-003 A9 (design §5.1): `storage` is now a required keyword-only
    constructor argument. This helper centralizes a working default (a
    `MagicMock` returning fake bytes) so individual tests only override
    what they actually care about."""
    kwargs = {"storage": storage if storage is not None else _fake_storage()}
    if batch_repository is not None:
        kwargs["batch_repository"] = batch_repository
    return AnalysisProcessor(
        photos if photos is not None else AsyncMock(),
        analysis if analysis is not None else AsyncMock(),
        analyzer if analyzer is not None else AsyncMock(),
        settings if settings is not None else _settings(),
        **kwargs,
    )


def _fake_response(
    faces_count=2,
    is_blurred=False,
    blur_score=0.1,
    perceptual_hash="abc123",
    eyes_closed_count=0,
    dominant_color="#ffffff",
    tags=None,
    model_version="stub/2.0.0",
):
    response = MagicMock()
    response.faces_count = faces_count
    response.is_blurred = is_blurred
    response.blur_score = blur_score
    response.perceptual_hash = perceptual_hash
    response.eyes_closed_count = eyes_closed_count
    response.dominant_color = dominant_color
    response.tags = tags if tags is not None else []
    response.model_version = model_version
    return response


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Never actually wait out the 1/2/4s backoff (or the
    UNAVAILABLE/DEADLINE_EXCEEDED cooldown) in tests."""
    sleep_calls: list[float] = []

    async def _fast_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(analysis_processor_module.asyncio, "sleep", _fast_sleep)
    return sleep_calls


@pytest.fixture(autouse=True)
def _fake_prepare_for_analysis(monkeypatch):
    """Replace the real Pillow-based `prepare_for_analysis` with a fast,
    deterministic stand-in so these tests never decode/encode an actual
    image - the downscale ladder itself is covered by
    `test_image_prep.py`. Returns a `PreparedImage` whose `.data` easily
    fits `ANALYZER_MAX_MESSAGE_BYTES`, so the preventive size check in
    `AnalysisProcessor._prepare_image` never trips unless a test
    overrides this fixture's target explicitly."""

    def _fake_prepare(data: bytes, settings: Settings) -> PreparedImage:
        return PreparedImage(data=_FAKE_PREPARED_BYTES, downscaled=False, width=64, height=64)

    monkeypatch.setattr(analysis_processor_module, "prepare_for_analysis", _fake_prepare)
    return _fake_prepare


class TestClaimSkip:
    async def test_rowcount_zero_skips_without_calling_analyzer_or_persisting(self):
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 0
        photos.get_batch_id.return_value = None
        analysis = AsyncMock()
        analyzer = AsyncMock()
        processor = _make_processor(photos, analysis, analyzer, _settings())
        session = AsyncMock()

        await processor.process(session, str(uuid.uuid4()), "photos/x/original.jpg")

        analyzer.analyze.assert_not_called()
        analysis.upsert.assert_not_called()
        photos.mark_done.assert_not_called()
        photos.mark_failed.assert_not_called()
        session.commit.assert_awaited_once()  # only the claim commit

    async def test_rowcount_zero_commits_the_claim_transaction(self):
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 0
        photos.get_batch_id.return_value = None
        processor = _make_processor(photos, AsyncMock(), AsyncMock(), _settings())
        session = AsyncMock()

        await processor.process(session, str(uuid.uuid4()), "key")

        photos.claim_for_processing.assert_awaited_once()
        session.commit.assert_awaited_once()

    async def test_rowcount_zero_never_downloads_from_storage(self):
        """Design §5.2 ("Почему качаем ПОСЛЕ захвата, а не до"): a message
        that lost the atomic claim (duplicate delivery / already terminal)
        must short-circuit before ANY MinIO read - downloading up to 50 MB
        just to discard it in the skip branch is exactly what the design
        argues against."""
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 0
        photos.get_batch_id.return_value = None
        storage = _fake_storage()
        processor = _make_processor(
            photos, AsyncMock(), AsyncMock(), _settings(), storage=storage
        )
        session = AsyncMock()

        await processor.process(session, str(uuid.uuid4()), "photos/x/original.jpg")

        storage.get_file.assert_not_called()


class TestSuccessPath:
    async def test_success_on_first_attempt_marks_done_and_upserts_result(self):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analysis = AsyncMock()
        analyzer = AsyncMock()
        analyzer.analyze.return_value = _fake_response(
            faces_count=4,
            blur_score=0.2,
            eyes_closed_count=1,
            dominant_color="#112233",
            tags=["face", "dark"],
            model_version="opencv-dnn-res10-ssd+laplacian+phash/1.1.0",
        )
        processor = _make_processor(photos, analysis, analyzer, _settings())
        session = AsyncMock()

        await processor.process(session, str(photo_id), "photos/x/original.jpg")

        analysis.upsert.assert_awaited_once()
        call = analysis.upsert.call_args
        assert call.args[1] == photo_id
        assert call.kwargs["faces_count"] == 4
        assert call.kwargs["blur_score"] == 0.2
        # TASK-003 A8: the four new analyzer fields flow through to `upsert`.
        assert call.kwargs["eyes_closed_count"] == 1
        assert call.kwargs["dominant_color"] == "#112233"
        assert call.kwargs["tags"] == ["face", "dark"]
        assert call.kwargs["model_version"] == "opencv-dnn-res10-ssd+laplacian+phash/1.1.0"

        photos.mark_done.assert_awaited_once_with(session, photo_id, 1)
        photos.mark_failed.assert_not_called()

    async def test_analyze_is_called_with_the_prepared_image_bytes(self):
        """TASK-003 A2/A8: the analyzer receives the OUTPUT of
        `prepare_for_analysis` (mocked here via the `_fake_prepare_for_analysis`
        fixture to `_FAKE_PREPARED_BYTES`), not the raw MinIO bytes."""
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        storage = _fake_storage(data=b"raw-bytes-from-minio")
        analyzer = AsyncMock()
        analyzer.analyze.return_value = _fake_response()
        processor = _make_processor(
            photos, AsyncMock(), analyzer, _settings(), storage=storage
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "photos/x/original.jpg")

        storage.get_file.assert_called_once_with("photos/x/original.jpg")
        analyzer.analyze.assert_awaited_once_with(
            str(photo_id), "photos/x/original.jpg", _FAKE_PREPARED_BYTES
        )

    async def test_image_is_downloaded_and_prepared_only_once_across_retries(self):
        """Design §5.2: the download+prepare step is cached across retry
        attempts - a transient gRPC failure must not re-download from
        MinIO nor re-run `prepare_for_analysis`."""
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        storage = _fake_storage()
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = [
            _rpc_error(grpc.StatusCode.UNAVAILABLE),
            _fake_response(),
        ]
        processor = _make_processor(
            photos, AsyncMock(), analyzer, _settings(), storage=storage
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        assert analyzer.analyze.await_count == 2
        storage.get_file.assert_called_once()  # NOT called again on the retry

    async def test_success_after_one_retryable_failure_reports_two_attempts(self):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analysis = AsyncMock()
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = [
            _rpc_error(grpc.StatusCode.UNAVAILABLE),
            _fake_response(),
        ]
        processor = _make_processor(photos, analysis, analyzer, _settings())
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        assert analyzer.analyze.await_count == 2
        photos.record_attempt.assert_awaited_once()
        record_call = photos.record_attempt.call_args
        assert record_call.args[1] == photo_id
        assert record_call.args[2] == 1  # attempts at time of the failed try
        assert record_call.args[3] == "UNAVAILABLE"

        photos.mark_done.assert_awaited_once_with(session, photo_id, 2)


class TestImagePreparation:
    """TASK-003 A8 (design §5.2-§5.3): the preventive size check, the
    downscale metric, and the storage-vs-analyzer metric split."""

    async def test_message_too_large_after_preparation_fails_no_retry(self, monkeypatch):
        """Even after downscaling, a payload that still doesn't fit
        `ANALYZER_MAX_MESSAGE_BYTES` must NOT be sent to the analyzer at
        all (design §4.1) and must fail as a permanent, single-attempt
        MESSAGE_TOO_LARGE - not exhaust all retries."""

        def _oversized_prepare(data, settings):
            return PreparedImage(data=b"x" * 10, downscaled=True, width=800, height=600)

        monkeypatch.setattr(
            analysis_processor_module, "prepare_for_analysis", _oversized_prepare
        )
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analyzer = AsyncMock()
        processor = _make_processor(
            photos, AsyncMock(), analyzer, _settings(WORKER_MAX_ATTEMPTS=3)
        )
        # Shrink the message budget so even a 10-byte payload trips the
        # preventive check, without needing a real oversized fixture image.
        processor._max_message_bytes = 5
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        analyzer.analyze.assert_not_called()  # no RPC attempted at all
        photos.mark_failed.assert_awaited_once()
        call = photos.mark_failed.call_args
        assert call.args[2] == "MESSAGE_TOO_LARGE"
        assert call.args[4] == 1  # exactly one attempt, no retries wasted

    async def test_downscaled_image_increments_the_downscaled_counter(self, monkeypatch):
        def _downscaled_prepare(data, settings):
            return PreparedImage(data=b"small", downscaled=True, width=800, height=600)

        monkeypatch.setattr(
            analysis_processor_module, "prepare_for_analysis", _downscaled_prepare
        )
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.return_value = _fake_response()
        processor = _make_processor(photos, AsyncMock(), analyzer, _settings())
        session = AsyncMock()
        before = _counter_value(analysis_processor_module.analyzer_image_downscaled_total)

        await processor.process(session, str(photo_id), "key")

        after = _counter_value(analysis_processor_module.analyzer_image_downscaled_total)
        assert after == before + 1

    async def test_storage_error_increments_storage_metric_not_analyzer_metric(self):
        """`NotFoundError`/`StorageUnavailable` from `ObjectStorage.get_file`
        must be counted on `storage_read_errors_total`, not
        `analyzer_grpc_errors_total` - they are not gRPC/analyzer failures
        (design A-8 step 5)."""
        from app.core.errors import StorageUnavailable

        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        storage = MagicMock()
        storage.get_file.side_effect = StorageUnavailable("MinIO is unreachable")
        analyzer = AsyncMock()
        processor = _make_processor(
            photos,
            AsyncMock(),
            analyzer,
            _settings(WORKER_MAX_ATTEMPTS=1),
            storage=storage,
        )
        session = AsyncMock()
        before = _counter_value(
            analysis_processor_module.storage_read_errors_total, code="STORAGE_UNAVAILABLE"
        )

        await processor.process(session, str(photo_id), "key")

        after = _counter_value(
            analysis_processor_module.storage_read_errors_total, code="STORAGE_UNAVAILABLE"
        )
        assert after == before + 1
        analyzer.analyze.assert_not_called()  # never got past the download step


class TestTimeoutsActuallyInterruptWaiting:
    """Spec criterion: "Таймаут вокруг чтения реально прерывает ожидание."

    Every other test in this file either uses a synchronous `MagicMock`
    that returns instantly, or the `_fake_prepare_for_analysis` autouse
    fixture - so the `asyncio.timeout(...)` wrappers around
    `anyio.to_thread.run_sync(...)` in `_prepare_image` are never actually
    exercised end-to-end anywhere else. These two tests use a REAL blocking
    call (`time.sleep` inside the synchronous callable, run on a real
    thread) and a real, tiny configured timeout, then assert on real
    wall-clock elapsed time - proving the timeout interrupts the awaiting
    coroutine well before the blocking call would finish on its own,
    rather than merely trusting the `asyncio.timeout(...)` call exists in
    the source (design §5.3: "asyncio.timeout вокруг to_thread отменяет
    ожидание, но не сам поток")."""

    async def test_storage_read_timeout_interrupts_a_slow_download(self):
        def _slow_get_file(_object_name: str) -> bytes:
            time.sleep(0.5)
            return b"too-late"

        storage = MagicMock()
        storage.get_file.side_effect = _slow_get_file
        settings = _settings().model_copy(update={"STORAGE_READ_TIMEOUT_SECONDS": 0.03})
        processor = _make_processor(
            AsyncMock(), AsyncMock(), AsyncMock(), settings, storage=storage
        )

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await processor._prepare_image("photo-1", "photos/x/original.jpg")
        elapsed = time.monotonic() - start

        # The blocking download sleeps 0.5s; the coroutine must be released
        # close to the configured 0.03s timeout, not after the full 0.5s.
        assert elapsed < 0.3

    async def test_image_prep_timeout_interrupts_a_slow_prepare_call(self, monkeypatch):
        def _slow_prepare(data: bytes, settings) -> PreparedImage:
            time.sleep(0.5)
            return PreparedImage(data=b"x", downscaled=False, width=1, height=1)

        # Overrides the autouse `_fake_prepare_for_analysis` fixture's patch
        # (applied second, so it wins) with a genuinely slow implementation.
        monkeypatch.setattr(analysis_processor_module, "prepare_for_analysis", _slow_prepare)
        settings = _settings().model_copy(update={"IMAGE_PREP_TIMEOUT_SECONDS": 0.03})
        processor = _make_processor(
            AsyncMock(), AsyncMock(), AsyncMock(), settings, storage=_fake_storage()
        )

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await processor._prepare_image("photo-1", "photos/x/original.jpg")
        elapsed = time.monotonic() - start

        assert elapsed < 0.3


class TestRetryExhaustion:
    async def test_retryable_error_every_attempt_marks_failed_with_retries_exhausted(self):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analysis = AsyncMock()
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = _rpc_error(grpc.StatusCode.UNAVAILABLE)
        processor = _make_processor(
            photos, analysis, analyzer, _settings(WORKER_MAX_ATTEMPTS=3)
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        assert analyzer.analyze.await_count == 3
        assert photos.record_attempt.await_count == 3
        analysis.upsert.assert_not_called()

        photos.mark_failed.assert_awaited_once()
        call = photos.mark_failed.call_args
        assert call.args[1] == photo_id
        assert call.args[2] == "UNAVAILABLE"  # last_error_code
        assert call.args[4] == 3  # attempts
        photos.mark_done.assert_not_called()

    async def test_attempts_column_grows_monotonically_across_retries(self):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = _rpc_error(grpc.StatusCode.DEADLINE_EXCEEDED)
        processor = _make_processor(
            photos, AsyncMock(), analyzer, _settings(WORKER_MAX_ATTEMPTS=3)
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        attempts_seen = [call.args[2] for call in photos.record_attempt.call_args_list]
        assert attempts_seen == [1, 2, 3]

    async def test_backoff_sleep_called_between_retries_not_after_last_attempt(self, _no_real_sleep):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = _rpc_error(grpc.StatusCode.UNAVAILABLE)
        processor = _make_processor(
            photos, AsyncMock(), analyzer, _settings(WORKER_MAX_ATTEMPTS=3, RETRY_BACKOFF_BASE_SECONDS=1.0)
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        # 3 attempts -> 2 in-loop backoff sleeps (between 1->2 and 2->3), none
        # after the final attempt. TASK-003 A8 (design §12 "вежливость"):
        # since the last error was UNAVAILABLE, a THIRD sleep call follows -
        # the post-loop cooldown, covered separately in
        # TestUnavailableCooldown below - so only assert the backoff-sequence
        # prefix here, not the full list.
        assert _no_real_sleep[:2] == [1.0, 2.0]


class TestUnavailableCooldown:
    """TASK-003 A8 (tasks/TASK-003/20_design.md §5.2 step 6, §12
    "вежливость к общему анализатору"): after retries are exhausted on a
    transient UNAVAILABLE/DEADLINE_EXCEEDED failure, the worker sleeps
    `ANALYZER_UNAVAILABLE_COOLDOWN_SECONDS` before returning - a simple
    throttle so a lengthy analyzer outage doesn't turn into a hot-loop."""

    async def test_cooldown_sleep_follows_exhausted_unavailable_retries(self, _no_real_sleep):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = _rpc_error(grpc.StatusCode.UNAVAILABLE)
        processor = _make_processor(
            photos,
            AsyncMock(),
            analyzer,
            _settings(
                WORKER_MAX_ATTEMPTS=3,
                RETRY_BACKOFF_BASE_SECONDS=1.0,
                ANALYZER_UNAVAILABLE_COOLDOWN_SECONDS=5.0,
            ),
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        assert _no_real_sleep == [1.0, 2.0, 5.0]

    async def test_no_cooldown_sleep_after_a_permanent_no_retry_failure(self, _no_real_sleep):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = _rpc_error(grpc.StatusCode.INVALID_ARGUMENT)
        processor = _make_processor(
            photos,
            AsyncMock(),
            analyzer,
            _settings(WORKER_MAX_ATTEMPTS=3, ANALYZER_UNAVAILABLE_COOLDOWN_SECONDS=5.0),
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        assert _no_real_sleep == []

    async def test_no_cooldown_sleep_after_a_successful_response(self, _no_real_sleep):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.return_value = _fake_response()
        processor = _make_processor(
            photos, AsyncMock(), analyzer, _settings(ANALYZER_UNAVAILABLE_COOLDOWN_SECONDS=5.0)
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        assert _no_real_sleep == []


class TestNoRetryPath:
    async def test_permanent_error_fails_immediately_without_retry(self):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = _rpc_error(grpc.StatusCode.INVALID_ARGUMENT)
        processor = _make_processor(
            photos, AsyncMock(), analyzer, _settings(WORKER_MAX_ATTEMPTS=3)
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        assert analyzer.analyze.await_count == 1  # no retry attempted
        photos.mark_failed.assert_awaited_once()
        call = photos.mark_failed.call_args
        assert call.args[2] == "INVALID_ARGUMENT"
        assert call.args[4] == 1  # attempts

    async def test_unexpected_exception_class_fails_immediately_fail_safe(self):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = ValueError("totally unexpected")
        processor = _make_processor(
            photos, AsyncMock(), analyzer, _settings(WORKER_MAX_ATTEMPTS=3)
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        assert analyzer.analyze.await_count == 1
        photos.mark_failed.assert_awaited_once()
        assert photos.mark_failed.call_args.args[2] == "UNKNOWN"


class TestMetricsAndCommitOrdering:
    async def test_every_terminal_write_is_followed_by_a_commit(self):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.return_value = _fake_response()
        processor = _make_processor(photos, AsyncMock(), analyzer, _settings())
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        # claim commit + terminal commit = 2
        assert session.commit.await_count == 2

    async def test_failure_path_also_commits_after_terminal_write(self):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = _rpc_error(grpc.StatusCode.INVALID_ARGUMENT)
        processor = _make_processor(
            photos, AsyncMock(), analyzer, _settings(WORKER_MAX_ATTEMPTS=3)
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        # claim commit + record_attempt commit + terminal (mark_failed) commit = 3
        assert session.commit.await_count == 3


def _batch_photo(
    status: PhotoStatus,
    *,
    is_blurred: bool = False,
    blur_score: float = 0.1,
    faces_count: int = 1,
    photo_id: uuid.UUID | None = None,
) -> Photo:
    """Same shape as `test_batch_service.py::_photo` - a real ORM `Photo`
    (not a `MagicMock`) so `select_best_photo`'s sort key comparisons
    (tuples of real values) work exactly as they would with rows loaded
    from the DB."""
    photo_id = photo_id or uuid.uuid4()
    photo = Photo(
        photo_id=photo_id,
        filename="a.jpg",
        object_key=f"photos/{photo_id}/original.jpg",
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    if status == PhotoStatus.done:
        photo.analysis = AnalysisResult(
            photo_id=photo_id,
            faces_count=faces_count,
            is_blurred=is_blurred,
            blur_score=blur_score,
            perceptual_hash="abc123",
        )
    else:
        photo.analysis = None
    return photo


def _fake_batch(photos: list[Photo], *, status: str = "processing", batch_id: uuid.UUID | None = None):
    batch = MagicMock()
    batch.batch_id = batch_id or uuid.uuid4()
    batch.status = status
    batch.photos = photos
    return batch


class TestMaybeCompleteBatchDirect:
    """Direct coverage of `AnalysisProcessor._maybe_complete_batch` (design
    §F3) - the no-op/short-circuit branches and the atomic-completion path,
    isolated from the surrounding `process()` retry/terminal-write logic."""

    async def test_no_batch_id_never_touches_the_batch_repository(self):
        photos = AsyncMock()
        photos.get_batch_id.return_value = None
        batches = AsyncMock()
        processor = _make_processor(
            photos, AsyncMock(), AsyncMock(), _settings(), batch_repository=batches
        )
        session = AsyncMock()

        await processor._maybe_complete_batch(session, uuid.uuid4())

        batches.get_by_id.assert_not_called()
        batches.try_complete.assert_not_called()
        session.commit.assert_not_called()

    async def test_missing_batch_row_is_a_noop(self):
        photos = AsyncMock()
        photos.get_batch_id.return_value = uuid.uuid4()
        batches = AsyncMock()
        batches.get_by_id.return_value = None
        processor = _make_processor(
            photos, AsyncMock(), AsyncMock(), _settings(), batch_repository=batches
        )

        await processor._maybe_complete_batch(AsyncMock(), uuid.uuid4())

        batches.try_complete.assert_not_called()

    async def test_batch_already_completed_is_a_noop(self):
        photos = AsyncMock()
        photos.get_batch_id.return_value = uuid.uuid4()
        batch = _fake_batch([_batch_photo(PhotoStatus.done)], status="completed")
        batches = AsyncMock()
        batches.get_by_id.return_value = batch
        processor = _make_processor(
            photos, AsyncMock(), AsyncMock(), _settings(), batch_repository=batches
        )

        await processor._maybe_complete_batch(AsyncMock(), uuid.uuid4())

        batches.try_complete.assert_not_called()

    async def test_one_photo_still_non_terminal_does_not_complete_the_batch(self):
        photos = AsyncMock()
        photos.get_batch_id.return_value = uuid.uuid4()
        batch = _fake_batch(
            [_batch_photo(PhotoStatus.done), _batch_photo(PhotoStatus.pending)]
        )
        batches = AsyncMock()
        batches.get_by_id.return_value = batch
        processor = _make_processor(
            photos, AsyncMock(), AsyncMock(), _settings(), batch_repository=batches
        )

        await processor._maybe_complete_batch(AsyncMock(), uuid.uuid4())

        batches.try_complete.assert_not_called()

    async def test_empty_photos_list_does_not_complete_the_batch(self):
        """`all()` over an empty list is vacuously True - guarded explicitly
        (same invariant as `BatchService.get_batch`, see
        test_batch_service.py)."""
        photos = AsyncMock()
        photos.get_batch_id.return_value = uuid.uuid4()
        batch = _fake_batch([])
        batches = AsyncMock()
        batches.get_by_id.return_value = batch
        processor = _make_processor(
            photos, AsyncMock(), AsyncMock(), _settings(), batch_repository=batches
        )

        await processor._maybe_complete_batch(AsyncMock(), uuid.uuid4())

        batches.try_complete.assert_not_called()

    async def test_all_terminal_completes_the_batch_with_the_computed_best_photo_id(self):
        best = _batch_photo(PhotoStatus.done, is_blurred=False, blur_score=0.1, faces_count=3)
        worse = _batch_photo(PhotoStatus.failed)
        batch = _fake_batch([worse, best])
        photos = AsyncMock()
        photos.get_batch_id.return_value = batch.batch_id
        batches = AsyncMock()
        batches.get_by_id.return_value = batch
        batches.try_complete.return_value = 1
        processor = _make_processor(
            photos, AsyncMock(), AsyncMock(), _settings(), batch_repository=batches
        )
        session = AsyncMock()

        await processor._maybe_complete_batch(session, uuid.uuid4())

        batches.try_complete.assert_awaited_once_with(session, batch.batch_id, best.photo_id)
        session.commit.assert_awaited_once()

    async def test_all_failed_completes_the_batch_with_null_best_photo_id(self):
        batch = _fake_batch([_batch_photo(PhotoStatus.failed), _batch_photo(PhotoStatus.failed)])
        photos = AsyncMock()
        photos.get_batch_id.return_value = batch.batch_id
        batches = AsyncMock()
        batches.get_by_id.return_value = batch
        batches.try_complete.return_value = 1
        processor = _make_processor(
            photos, AsyncMock(), AsyncMock(), _settings(), batch_repository=batches
        )
        session = AsyncMock()

        await processor._maybe_complete_batch(session, uuid.uuid4())

        batches.try_complete.assert_awaited_once_with(session, batch.batch_id, None)

    async def test_lost_race_rowcount_zero_does_not_raise_and_still_commits(self):
        """TASK-002.1 F3: two workers finishing the last two photos of a
        batch near-simultaneously may both call `try_complete` - the loser
        gets `rowcount=0` (no row matched `WHERE status='processing'`
        anymore) and must treat that as a harmless no-op, not an error."""
        batch = _fake_batch([_batch_photo(PhotoStatus.done)])
        photos = AsyncMock()
        photos.get_batch_id.return_value = batch.batch_id
        batches = AsyncMock()
        batches.get_by_id.return_value = batch
        batches.try_complete.return_value = 0  # lost the race
        processor = _make_processor(
            photos, AsyncMock(), AsyncMock(), _settings(), batch_repository=batches
        )
        session = AsyncMock()

        await processor._maybe_complete_batch(session, uuid.uuid4())  # must not raise

        session.commit.assert_awaited_once()  # still commits the (no-op) UPDATE


class TestMaybeCompleteBatchCalledOnAllTerminalPaths:
    """TASK-002.1 F3: `_maybe_complete_batch` must run after EVERY terminal
    write - the duplicate/self-healing skip path, the success (`done`)
    path, and the (`failed`) path - not just one or two of them. Each test
    proves the call happened by making `get_batch_id` return a real batch
    id and asserting the batch repository was actually consulted."""

    async def test_skip_duplicate_path_calls_maybe_complete_batch(self):
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 0  # already claimed/terminal
        batch_id = uuid.uuid4()
        photos.get_batch_id.return_value = batch_id
        batches = AsyncMock()
        batches.get_by_id.return_value = None  # short-circuits after the lookup
        processor = _make_processor(
            photos, AsyncMock(), AsyncMock(), _settings(), batch_repository=batches
        )
        session = AsyncMock()

        await processor.process(session, str(uuid.uuid4()), "key")

        photos.get_batch_id.assert_awaited_once_with(session, ANY)
        batches.get_by_id.assert_awaited_once_with(session, batch_id)

    async def test_done_path_calls_maybe_complete_batch(self):
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        batch_id = uuid.uuid4()
        photos.get_batch_id.return_value = batch_id
        batches = AsyncMock()
        batches.get_by_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.return_value = _fake_response()
        processor = _make_processor(
            photos, AsyncMock(), analyzer, _settings(), batch_repository=batches
        )
        session = AsyncMock()

        await processor.process(session, str(uuid.uuid4()), "key")

        batches.get_by_id.assert_awaited_once_with(session, batch_id)

    async def test_failed_path_calls_maybe_complete_batch(self):
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        batch_id = uuid.uuid4()
        photos.get_batch_id.return_value = batch_id
        batches = AsyncMock()
        batches.get_by_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = _rpc_error(grpc.StatusCode.INVALID_ARGUMENT)
        processor = _make_processor(
            photos,
            AsyncMock(),
            analyzer,
            _settings(WORKER_MAX_ATTEMPTS=3),
            batch_repository=batches,
        )
        session = AsyncMock()

        await processor.process(session, str(uuid.uuid4()), "key")

        batches.get_by_id.assert_awaited_once_with(session, batch_id)


class TestWorkerMessagesProcessedTotalNotDoubleCounted:
    """TASK-002.1 review-1 m4: `worker_messages_processed_total` must count
    *messages*, not be incremented while the message's processing (which
    includes `_maybe_complete_batch`) might still fail. If
    `_maybe_complete_batch` raises, the exception must propagate (so the
    message stays uncommitted and is redelivered by `consumer.consume_loop`)
    AND the increment for this attempt must NOT have happened - otherwise a
    later successful retry of the same message would double-count it."""

    async def test_skip_path_maybe_complete_batch_raises_does_not_increment_metric(self):
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 0  # already claimed/terminal -> skip path
        photos.get_batch_id.side_effect = RuntimeError("db blip")
        processor = _make_processor(photos, AsyncMock(), AsyncMock(), _settings())
        session = AsyncMock()
        before = _counter_value(worker_messages_processed_total, result="skipped")

        with pytest.raises(RuntimeError):
            await processor.process(session, str(uuid.uuid4()), "key")

        after = _counter_value(worker_messages_processed_total, result="skipped")
        assert after == before

    async def test_done_path_maybe_complete_batch_raises_does_not_increment_metric(self):
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.side_effect = RuntimeError("db blip")
        analyzer = AsyncMock()
        analyzer.analyze.return_value = _fake_response()
        processor = _make_processor(photos, AsyncMock(), analyzer, _settings())
        session = AsyncMock()
        before = _counter_value(worker_messages_processed_total, result="done")

        with pytest.raises(RuntimeError):
            await processor.process(session, str(uuid.uuid4()), "key")

        after = _counter_value(worker_messages_processed_total, result="done")
        assert after == before

    async def test_failed_path_maybe_complete_batch_raises_does_not_increment_metric(self):
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.side_effect = RuntimeError("db blip")
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = _rpc_error(grpc.StatusCode.INVALID_ARGUMENT)
        processor = _make_processor(
            photos, AsyncMock(), analyzer, _settings(WORKER_MAX_ATTEMPTS=3)
        )
        session = AsyncMock()
        before = _counter_value(worker_messages_processed_total, result="failed")

        with pytest.raises(RuntimeError):
            await processor.process(session, str(uuid.uuid4()), "key")

        after = _counter_value(worker_messages_processed_total, result="failed")
        assert after == before
