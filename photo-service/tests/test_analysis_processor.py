"""Behavioural coverage of `app.services.analysis_processor.AnalysisProcessor`.

Design §5.3 / §12 step 11: claim -> bounded gRPC retry loop -> terminal
write. `PhotoRepository`, `AnalysisResultRepository` and the gRPC
`AnalyzerGrpcClient` are always mocked here - no real DB/Kafka/gRPC
connection is made (the real analyzer is never called, per the
test-writer brief). `asyncio.sleep` between retries is monkeypatched to a
no-op so the retry-exhaustion tests run instantly instead of waiting the
real 1s/2s/4s backoff.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest

from app.core.config import Settings
from app.services import analysis_processor as analysis_processor_module
from app.services.analysis_processor import AnalysisProcessor


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
        analyzer = AsyncMock()
        analyzer.analyze.side_effect = _rpc_error(grpc.StatusCode.INVALID_ARGUMENT)
        processor = AnalysisProcessor(
            photos, AsyncMock(), analyzer, _settings(WORKER_MAX_ATTEMPTS=3)
        )
        session = AsyncMock()

        await processor.process(session, str(photo_id), "key")

        # claim commit + record_attempt commit + terminal (mark_failed) commit = 3
        assert session.commit.await_count == 3
