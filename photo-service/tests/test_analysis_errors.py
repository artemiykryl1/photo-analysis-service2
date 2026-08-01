"""Behavioural coverage of `app.services.analysis_errors.classify_error`
(TASK-003 A7, tasks/TASK-003/20_design.md §4.2/§4.3).

A pure function - every branch of the classification table is exercised
directly against real exception instances, no mocks/I/O needed.
"""

import grpc
import urllib3.exceptions

from app.core.errors import NotFoundError, StorageUnavailable
from app.integrations.analyzer_client import AnalyzerMessageTooLarge, RetryDecision
from app.services.analysis_errors import classify_error
from app.services.image_prep import ImageDecodeError, ImageTooLargeError


def _rpc_error(code: grpc.StatusCode, details: str = "boom") -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(
        code=code,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details=details,
    )


class TestStorageErrors:
    def test_not_found_error_is_no_retry_object_not_found(self):
        """TASK-003 A2 narrowing: MinIO's `NoSuchKey` (mapped to
        `NotFoundError` by `ObjectStorage.get_file`) is a PERMANENT failure
        - strong read-after-write consistency means the object is not
        coming back on a later attempt."""
        decision, code = classify_error(NotFoundError("Object not found"))
        assert decision == RetryDecision.NO_RETRY
        assert code == "OBJECT_NOT_FOUND"

    def test_storage_unavailable_is_retryable(self):
        decision, code = classify_error(StorageUnavailable("MinIO is unreachable"))
        assert decision == RetryDecision.RETRY
        assert code == "STORAGE_UNAVAILABLE"

    def test_raw_urllib3_transport_error_is_retryable(self):
        """Review-1 BLOCKING-1 regression test: a real MinIO outage never
        raises `S3Error` (that requires MinIO to have already answered) - it
        raises a raw `urllib3` transport error such as `MaxRetryError`.
        `ObjectStorage.get_file` now converts this to `StorageUnavailable`
        before `classify_error` ever sees it (see
        `test_analyzer_client.py`/`test_analysis_processor.py` for the
        storage-layer proof); this test locks in the defense-in-depth branch
        here directly, so a raw transport error is never misclassified as
        the fail-safe `NO_RETRY`/`UNKNOWN`."""
        exc = urllib3.exceptions.MaxRetryError(pool=None, url="http://minio:9000")
        decision, code = classify_error(exc)
        assert decision == RetryDecision.RETRY
        assert code == "STORAGE_UNAVAILABLE"

    def test_raw_connection_error_is_retryable(self):
        decision, code = classify_error(ConnectionError("connection refused"))
        assert decision == RetryDecision.RETRY
        assert code == "STORAGE_UNAVAILABLE"


class TestImageErrors:
    def test_image_decode_error_is_no_retry(self):
        decision, code = classify_error(ImageDecodeError("could not decode"))
        assert decision == RetryDecision.NO_RETRY
        assert code == "IMAGE_DECODE_FAILED"

    def test_image_too_large_error_is_no_retry(self):
        decision, code = classify_error(ImageTooLargeError("too many pixels"))
        assert decision == RetryDecision.NO_RETRY
        assert code == "IMAGE_TOO_LARGE"


class TestDelegatedToAnalyzerClient:
    """Everything else is delegated to
    `app.integrations.analyzer_client.classify_grpc_error`/
    `error_code_from_exception` - this includes gRPC statuses,
    `TimeoutError`, and the two message-too-large cases (design §4.1),
    which are already covered in depth by `test_analyzer_client.py`. Only
    a representative sample is re-checked here to confirm the delegation
    itself is wired correctly.
    """

    def test_analyzer_message_too_large_is_no_retry(self):
        decision, code = classify_error(AnalyzerMessageTooLarge(sent_bytes=5_000_000, limit=4_194_304))
        assert decision == RetryDecision.NO_RETRY
        assert code == "MESSAGE_TOO_LARGE"

    def test_resource_exhausted_with_size_marker_is_no_retry(self):
        exc = _rpc_error(
            grpc.StatusCode.RESOURCE_EXHAUSTED, "Received message larger than max (1 vs 2)"
        )
        decision, code = classify_error(exc)
        assert decision == RetryDecision.NO_RETRY
        assert code == "MESSAGE_TOO_LARGE"

    def test_resource_exhausted_without_size_marker_is_retryable(self):
        exc = _rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED, "rate limited")
        decision, code = classify_error(exc)
        assert decision == RetryDecision.RETRY
        assert code == "RESOURCE_EXHAUSTED"

    def test_unavailable_is_retryable(self):
        decision, code = classify_error(_rpc_error(grpc.StatusCode.UNAVAILABLE))
        assert decision == RetryDecision.RETRY
        assert code == "UNAVAILABLE"

    def test_invalid_argument_is_no_retry(self):
        decision, code = classify_error(_rpc_error(grpc.StatusCode.INVALID_ARGUMENT))
        assert decision == RetryDecision.NO_RETRY
        assert code == "INVALID_ARGUMENT"

    def test_timeout_error_is_retryable(self):
        decision, code = classify_error(TimeoutError("deadline"))
        assert decision == RetryDecision.RETRY
        assert code == "TIMEOUT"

    def test_unexpected_exception_is_no_retry_unknown(self):
        decision, code = classify_error(ValueError("totally unexpected"))
        assert decision == RetryDecision.NO_RETRY
        assert code == "UNKNOWN"
