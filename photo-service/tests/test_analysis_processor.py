"""Behavioural coverage of `app.services.analysis_processor.AnalysisProcessor`.

Design §5.3 / §12 step 11: claim -> bounded gRPC retry loop -> terminal
write. `PhotoRepository`, `AnalysisResultRepository` and the gRPC
`AnalyzerGrpcClient` are always mocked here - no real DB/Kafka/gRPC
connection is made (the real analyzer is never called, per the
test-writer brief). `asyncio.sleep` between retries is monkeypatched to a
no-op so the retry-exhaustion tests run instantly instead of waiting the
real 1s/2s/4s backoff.

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
    )


def _fake_response(faces_count=2, is_blurred=False, blur_score=0.1, perceptual_hash="abc123"):
    response = MagicMock()
    response.faces_count = faces_count
    response.is_blurred = is_blurred
    response.blur_score = blur_score
    response.perceptual_hash = perceptual_hash
    return response


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Never actually wait out the 1/2/4s backoff in tests."""
    sleep_calls: list[float] = []

    async def _fast_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(analysis_processor_module.asyncio, "sleep", _fast_sleep)
    return sleep_calls


class TestClaimSkip:
    async def test_rowcount_zero_skips_without_calling_analyzer_or_persisting(self):
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 0
        photos.get_batch_id.return_value = None
        analysis = AsyncMock()
        analyzer = AsyncMock()
        processor = AnalysisProcessor(photos, analysis, analyzer, _settings())
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
        processor = AnalysisProcessor(photos, AsyncMock(), AsyncMock(), _settings())
        session = AsyncMock()

        await processor.process(session, str(uuid.uuid4()), "key")

        photos.claim_for_processing.assert_awaited_once()
        session.commit.assert_awaited_once()


class TestSuccessPath:
    async def test_success_on_first_attempt_marks_done_and_upserts_result(self):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analysis = AsyncMock()
        analyzer = AsyncMock()
        analyzer.analyze.return_value = _fake_response(faces_count=4, blur_score=0.2)
        processor = AnalysisProcessor(photos, analysis, analyzer, _settings())
        session = AsyncMock()

        await processor.process(session, str(photo_id), "photos/x/original.jpg")

        analysis.upsert.assert_awaited_once()
        call = analysis.upsert.call_args
        assert call.args[1] == photo_id
        assert call.kwargs["faces_count"] == 4
        assert call.kwargs["blur_score"] == 0.2

        photos.mark_done.assert_awaited_once_with(session, photo_id, 1)
        photos.mark_failed.assert_not_called()

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
        processor = AnalysisProcessor(photos, analysis, analyzer, _settings())
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        assert analyzer.analyze.await_count == 2
        photos.record_attempt.assert_awaited_once()
        record_call = photos.record_attempt.call_args
        assert record_call.args[1] == photo_id
        assert record_call.args[2] == 1  # attempts at time of the failed try
        assert record_call.args[3] == "UNAVAILABLE"

        photos.mark_done.assert_awaited_once_with(session, photo_id, 2)


class TestRetryExhaustion:
    async def test_retryable_error_every_attempt_marks_failed_with_retries_exhausted(self):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analysis = AsyncMock()
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = _rpc_error(grpc.StatusCode.UNAVAILABLE)
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
            photos, AsyncMock(), analyzer, _settings(WORKER_MAX_ATTEMPTS=3, RETRY_BACKOFF_BASE_SECONDS=1.0)
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        # 3 attempts -> 2 sleeps (between 1->2 and 2->3), none after the final failure.
        assert _no_real_sleep == [1.0, 2.0]


class TestNoRetryPath:
    async def test_permanent_error_fails_immediately_without_retry(self):
        photo_id = uuid.uuid4()
        photos = AsyncMock()
        photos.claim_for_processing.return_value = 1
        photos.get_batch_id.return_value = None
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = _rpc_error(grpc.StatusCode.INVALID_ARGUMENT)
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(photos, AsyncMock(), analyzer, _settings())
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(
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
        processor = AnalysisProcessor(photos, AsyncMock(), AsyncMock(), _settings())
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
        processor = AnalysisProcessor(photos, AsyncMock(), analyzer, _settings())
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
        processor = AnalysisProcessor(
            photos, AsyncMock(), analyzer, _settings(WORKER_MAX_ATTEMPTS=3)
        )
        session = AsyncMock()
        before = _counter_value(worker_messages_processed_total, result="failed")

        with pytest.raises(RuntimeError):
            await processor.process(session, str(uuid.uuid4()), "key")

        after = _counter_value(worker_messages_processed_total, result="failed")
        assert after == before
