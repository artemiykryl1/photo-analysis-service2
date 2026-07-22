"""gRPC client for the Analyzer Service (real service in prod, analyzer-stub
in dev/CI - both implement `protos/analyzer.proto`, `package analyzer.v1`).

Real invocation path for TASK-002 (tasks/TASK-002/20_design.md §7.2):
the worker (`app/worker/`) calls `AnalyzerGrpcClient.analyze()` for every
`photo.analysis.requested` Kafka message it claims. The channel is created
once at worker startup and closed on graceful shutdown - it is NOT
per-message.

`classify_grpc_error` implements the retry/no-retry table from design
§5.4, extended by TASK-003 A4 (tasks/TASK-003/20_design.md §4). It is a
pure function (no I/O) so it can be unit-tested against every gRPC status
code without a running server.

TASK-003 A3/A4 (spike finding: tasks/TASK-003/05_spike_analyzer.md finding
2): the real analyzer's 4 MiB message ceiling surfaces as
`RESOURCE_EXHAUSTED`, the SAME status code used for transient throttling.
Retrying "message too large" three times is pure waste (it will never
succeed) and produces an unhelpful `last_error_code`. `_is_message_too_large`
distinguishes the two by inspecting the RPC's `details()` text for the
server's/client's own wording - this is the second-layer safety net;
`AnalysisProcessor` mainly avoids the situation altogether via a preventive
size check before ever calling `analyze()` (design §4.1).
"""

import enum
import logging

import grpc

from app.grpc_gen import analyzer_pb2, analyzer_pb2_grpc

logger = logging.getLogger(__name__)

MAX_ERROR_MESSAGE_LENGTH = 500  # constitution.md §3.3: never log/store unbounded payloads

# gRPC statuses that indicate a transient condition worth retrying.
_RETRYABLE_GRPC_CODES = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.ABORTED,
        grpc.StatusCode.INTERNAL,
    }
)

# TASK-003 A4 (design §4.1): case-insensitive marker distinguishing a
# permanent "message too large" RESOURCE_EXHAUSTED from a transient
# throttling RESOURCE_EXHAUSTED. Covers both wordings gRPC is known to use
# ("Received message larger than max" server-side, "Sent message larger
# than max" client-side) since only the "larger than max" fragment is
# common to both.
_MESSAGE_TOO_LARGE_MARKER = "larger than max"


def _is_message_too_large(exc: Exception) -> bool:
    """True if `exc` is a `RESOURCE_EXHAUSTED` RPC error whose detail text
    indicates the message exceeded the server's/client's size limit (as
    opposed to e.g. the shared analyzer throttling under load)."""
    return (
        isinstance(exc, grpc.aio.AioRpcError)
        and exc.code() is grpc.StatusCode.RESOURCE_EXHAUSTED
        and _MESSAGE_TOO_LARGE_MARKER in (exc.details() or "").lower()
    )


class AnalyzerMessageTooLarge(Exception):
    """Raised by `AnalysisProcessor` (preventive check, design §4.1) when a
    prepared image still would not fit `ANALYZER_MAX_MESSAGE_BYTES` - a
    permanent condition, classified NO_RETRY with `last_error_code =
    MESSAGE_TOO_LARGE` (design §4.2). No RPC is attempted in this case."""

    def __init__(self, sent_bytes: int, limit: int) -> None:
        self.sent_bytes = sent_bytes
        self.limit = limit
        super().__init__(
            f"prepared image is {sent_bytes} bytes, exceeds the analyzer's "
            f"{limit}-byte message limit"
        )


class RetryDecision(str, enum.Enum):
    RETRY = "retry"
    NO_RETRY = "no_retry"


class AnalyzerGrpcClient:
    """Thin async wrapper around the generated `PhotoAnalyzerStub`."""

    def __init__(self, addr: str, timeout: float) -> None:
        self._addr = addr
        self._timeout = timeout
        # TASK-002.1 (tasks/TASK-002.1/20_design.md F7) deferred this
        # decision to TASK-003, once a real analyzer arrived over an
        # external network, to define the actual trust boundary. TASK-003
        # A8 (tasks/TASK-003/20_design.md §1, spike A3:
        # tasks/TASK-003/05_spike_analyzer.md) closes that deferral
        # definitively: the real analyzer at 45.132.19.101:50051 is
        # confirmed plaintext/insecure (no TLS offered), so
        # `insecure_channel` is the final choice, not a stopgap. No
        # `ANALYZER_GRPC_TLS` env var will be introduced - there is nothing
        # on the other end to negotiate TLS with.
        self._channel = grpc.aio.insecure_channel(addr)
        self._stub = analyzer_pb2_grpc.PhotoAnalyzerStub(self._channel)

    async def analyze(
        self, photo_id: str, object_key: str, image_bytes: bytes
    ) -> analyzer_pb2.AnalyzePhotoResponse:
        """Call `PhotoAnalyzer.AnalyzePhoto`. Raises on any RPC failure -
        callers (AnalysisProcessor) classify the exception via
        `classify_error` (app.services.analysis_errors), which delegates
        gRPC-specific classification back to `classify_grpc_error` below.

        TASK-003 A2/A1: `image_bytes` is the analyzer-ready payload
        prepared by `app.services.image_prep.prepare_for_analysis` - the
        real analyzer requires the actual image content, not just
        `photo_id`/`object_key` (tasks/TASK-003/20_design.md §2).
        """
        request = analyzer_pb2.AnalyzePhotoRequest(
            photo_id=photo_id, object_key=object_key, image_bytes=image_bytes
        )
        return await self._stub.AnalyzePhoto(request, timeout=self._timeout)

    async def close(self) -> None:
        await self._channel.close()


def classify_grpc_error(exc: Exception) -> RetryDecision:
    """Classify an exception raised by `AnalyzerGrpcClient.analyze()` (or
    the `asyncio.timeout()` wrapper around it) into retry/no-retry, per
    tasks/TASK-002/20_design.md §5.4, extended by TASK-003 A4
    (tasks/TASK-003/20_design.md §4.2).

    - `AnalyzerMessageTooLarge` (the preventive size check) and a
      `RESOURCE_EXHAUSTED` whose detail indicates the message itself was
      too large -> NO_RETRY (`MESSAGE_TOO_LARGE`, permanent - see
      `_is_message_too_large`).
    - Other transient gRPC statuses (UNAVAILABLE, DEADLINE_EXCEEDED,
      RESOURCE_EXHAUSTED from throttling, ABORTED, INTERNAL) and
      `TimeoutError` -> RETRY.
    - Permanent gRPC statuses (INVALID_ARGUMENT, NOT_FOUND,
      FAILED_PRECONDITION, UNIMPLEMENTED, PERMISSION_DENIED,
      UNAUTHENTICATED, OUT_OF_RANGE) -> NO_RETRY.
    - Anything else (unexpected) -> NO_RETRY (fail-safe: never retry an
      error class we don't understand, to avoid an infinite/looping retry).
    """
    if isinstance(exc, AnalyzerMessageTooLarge):
        return RetryDecision.NO_RETRY

    if isinstance(exc, TimeoutError):
        return RetryDecision.RETRY

    if isinstance(exc, grpc.aio.AioRpcError):
        if _is_message_too_large(exc):
            return RetryDecision.NO_RETRY
        if exc.code() in _RETRYABLE_GRPC_CODES:
            return RetryDecision.RETRY
        return RetryDecision.NO_RETRY

    return RetryDecision.NO_RETRY


def error_code_from_exception(exc: Exception) -> str:
    """Best-effort short code for `photos.last_error_code` (design §5.4,
    extended by TASK-003 A4/design §4.2 for the message-too-large case -
    both call sites of the "is this too-large" check MUST agree, or the
    retry decision and the stored error code would disagree with each
    other)."""
    if isinstance(exc, AnalyzerMessageTooLarge):
        return "MESSAGE_TOO_LARGE"
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    if isinstance(exc, grpc.aio.AioRpcError):
        if _is_message_too_large(exc):
            return "MESSAGE_TOO_LARGE"
        return exc.code().name
    return "UNKNOWN"


def truncate_error_message(message: str, limit: int = MAX_ERROR_MESSAGE_LENGTH) -> str:
    """Truncate an error message for `photos.last_error_message` - never
    store unbounded text (constitution.md §3.3), and this only ever
    carries the exception's `str()`, not file contents.
    """
    if len(message) <= limit:
        return message
    return message[:limit]
