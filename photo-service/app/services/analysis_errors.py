"""Unified error classification for `AnalysisProcessor` (TASK-003 A7,
tasks/TASK-003/20_design.md §4.3).

`app.integrations.analyzer_client.classify_grpc_error`/
`error_code_from_exception` only know about gRPC statuses and timeouts -
that scope is deliberately unchanged (its existing tests keep passing
unmodified). TASK-003 adds two new failure sources the worker must also
classify: `ObjectStorage.get_file` (MinIO) and
`app.services.image_prep.prepare_for_analysis` (Pillow). Mixing those into
`analyzer_client` would blur a layer boundary that has nothing to do with
gRPC, so this module is the single place `AnalysisProcessor` calls into for
retry classification - it handles the storage/image cases itself and
delegates everything else (including the two gRPC message-too-large cases)
to `analyzer_client`.

This is a pure function: no I/O, no Settings, trivially unit-testable
against every branch of the table in design §4.2.
"""

import urllib3.exceptions

from app.core.errors import NotFoundError, StorageUnavailable
from app.integrations.analyzer_client import (
    RetryDecision,
    classify_grpc_error,
    error_code_from_exception,
)
from app.services.image_prep import ImageDecodeError, ImageTooLargeError


def classify_error(exc: Exception) -> tuple[RetryDecision, str]:
    """Return `(retry_decision, last_error_code)` for an exception raised
    anywhere in `AnalysisProcessor`'s per-attempt try block (design §4.2):

    - `NotFoundError` (MinIO `NoSuchKey`) -> `(NO_RETRY, OBJECT_NOT_FOUND)`.
      **Deliberate narrowing of TASK-003 A2's "storage read errors are
      retryable"**: if the object is gone, MinIO's strong read-after-write
      consistency means it is not coming back - a 404 will not turn into a
      200 on the next attempt, unlike a network blip or a temporarily
      overloaded MinIO (which raise `StorageUnavailable` below and ARE
      retried).
    - `StorageUnavailable` (any other MinIO failure) -> `(RETRY,
      STORAGE_UNAVAILABLE)` - transient by nature (design A2).
    - `ImageDecodeError` (Pillow could not decode the bytes) -> `(NO_RETRY,
      IMAGE_DECODE_FAILED)` - the same bytes will never decode differently.
    - `ImageTooLargeError` (pixel-count guard, or no downscale ladder step
      fit the analyzer's byte budget) -> `(NO_RETRY, IMAGE_TOO_LARGE)`.
    - Everything else (gRPC statuses, `TimeoutError`,
      `AnalyzerMessageTooLarge`, and any unexpected exception) is delegated
      to `analyzer_client.classify_grpc_error`/`error_code_from_exception`,
      which already implements the MESSAGE_TOO_LARGE-vs-RESOURCE_EXHAUSTED
      split (TASK-003 A4) and the fail-safe NO_RETRY/UNKNOWN default.
    """
    if isinstance(exc, NotFoundError):
        return RetryDecision.NO_RETRY, "OBJECT_NOT_FOUND"
    if isinstance(exc, StorageUnavailable):
        return RetryDecision.RETRY, "STORAGE_UNAVAILABLE"
    if isinstance(exc, (urllib3.exceptions.HTTPError, ConnectionError)):
        # Defense in depth (review-1 BLOCKING-1): `ObjectStorage` already
        # converts every MinIO transport failure into `StorageUnavailable`
        # before it reaches here, so this branch should be unreachable in
        # practice. It stays as a second line of defense in case a future
        # caller (or a future version of the `minio`/`urllib3` stack) raises
        # a raw transport error directly - such a failure must never fall
        # through to the fail-safe `NO_RETRY`/`UNKNOWN` default below.
        # Deliberately NOT bare `OSError`: builtin `TimeoutError` is also an
        # `OSError` subclass and must keep classifying as `TIMEOUT` (see
        # `test_timeout_error_is_retryable` below), not `STORAGE_UNAVAILABLE`.
        return RetryDecision.RETRY, "STORAGE_UNAVAILABLE"
    if isinstance(exc, ImageDecodeError):
        return RetryDecision.NO_RETRY, "IMAGE_DECODE_FAILED"
    if isinstance(exc, ImageTooLargeError):
        return RetryDecision.NO_RETRY, "IMAGE_TOO_LARGE"

    return classify_grpc_error(exc), error_code_from_exception(exc)
