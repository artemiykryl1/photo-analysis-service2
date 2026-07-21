"""gRPC client for the (external, stubbed in dev) Analyzer Service.

Real invocation path for TASK-002 (tasks/TASK-002/20_design.md §7.2):
the worker (`app/worker/`) calls `AnalyzerGrpcClient.analyze()` for every
`photo.analysis.requested` Kafka message it claims. The channel is created
once at worker startup and closed on graceful shutdown - it is NOT
per-message.

`classify_grpc_error` implements the retry/no-retry table from design
§5.4. It is a pure function (no I/O) so it can be unit-tested against
every gRPC status code without a running server.
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


class RetryDecision(str, enum.Enum):
    RETRY = "retry"
    NO_RETRY = "no_retry"


class AnalyzerGrpcClient:
    """Thin async wrapper around the generated `PhotoAnalyzerStub`."""

    def __init__(self, addr: str, timeout: float) -> None:
        self._addr = addr
        self._timeout = timeout
        # TASK-002.1 (tasks/TASK-002.1/20_design.md F7): `insecure_channel`
        # is a deliberate, documented choice for the current MVP topology,
        # not an oversight - `worker` and `analyzer-stub` both live inside
        # the same single docker-compose network with no untrusted
        # participants. Deferred to TASK-003 (rather than added here):
        # configurable TLS (`ANALYZER_GRPC_TLS` + a root-cert path) once a
        # real analyzer arrives over an external network, where the actual
        # trust boundary/certificate model will be defined. No new env var
        # is introduced in TASK-002.1 to avoid shipping dead configuration
        # for a plaintext-only stub.
        self._channel = grpc.aio.insecure_channel(addr)
        self._stub = analyzer_pb2_grpc.PhotoAnalyzerStub(self._channel)

    async def analyze(
        self, photo_id: str, object_key: str
    ) -> analyzer_pb2.AnalyzePhotoResponse:
        """Call `PhotoAnalyzer.AnalyzePhoto`. Raises on any RPC failure -
        callers (AnalysisProcessor) classify the exception via
        `classify_grpc_error`.
        """
        request = analyzer_pb2.AnalyzePhotoRequest(photo_id=photo_id, object_key=object_key)
        return await self._stub.AnalyzePhoto(request, timeout=self._timeout)

    async def close(self) -> None:
        await self._channel.close()


def classify_grpc_error(exc: Exception) -> RetryDecision:
    """Classify an exception raised by `AnalyzerGrpcClient.analyze()` (or
    the `asyncio.timeout()` wrapper around it) into retry/no-retry, per
    tasks/TASK-002/20_design.md §5.4.

    - Transient gRPC statuses (UNAVAILABLE, DEADLINE_EXCEEDED,
      RESOURCE_EXHAUSTED, ABORTED, INTERNAL) and `TimeoutError` -> RETRY.
    - Permanent gRPC statuses (INVALID_ARGUMENT, NOT_FOUND,
      FAILED_PRECONDITION, UNIMPLEMENTED, PERMISSION_DENIED,
      UNAUTHENTICATED, OUT_OF_RANGE) -> NO_RETRY.
    - Anything else (unexpected) -> NO_RETRY (fail-safe: never retry an
      error class we don't understand, to avoid an infinite/looping retry).
    """
    if isinstance(exc, TimeoutError):
        return RetryDecision.RETRY

    if isinstance(exc, grpc.aio.AioRpcError):
        if exc.code() in _RETRYABLE_GRPC_CODES:
            return RetryDecision.RETRY
        return RetryDecision.NO_RETRY

    return RetryDecision.NO_RETRY


def error_code_from_exception(exc: Exception) -> str:
    """Best-effort short code for `photos.last_error_code` (design §5.4)."""
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    if isinstance(exc, grpc.aio.AioRpcError):
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
