"""Behavioural coverage of `app.integrations.analyzer_client`.

`classify_grpc_error`/`error_code_from_exception`/`truncate_error_message`
are pure functions (design §5.4/§7.2) - exercised here against every gRPC
status code in the retry/no-retry table, plus `TimeoutError` and an
"unexpected" exception class (fail-safe -> NO_RETRY). `AnalyzerGrpcClient`
itself is covered at the construction/close level only - the real RPC call
is exercised indirectly through `AnalysisProcessor` tests
(`test_analysis_processor.py`), which mock the client entirely (no live
gRPC server in this suite, per the test-writer brief: never call the real
analyzer).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest

from app.integrations.analyzer_client import (
    MAX_ERROR_MESSAGE_LENGTH,
    AnalyzerGrpcClient,
    RetryDecision,
    classify_grpc_error,
    error_code_from_exception,
    truncate_error_message,
)


def _rpc_error(code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(
        code=code,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details=f"{code.name} from stub",
    )


class TestClassifyGrpcErrorRetryable:
    @pytest.mark.parametrize(
        "code",
        [
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.DEADLINE_EXCEEDED,
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            grpc.StatusCode.ABORTED,
            grpc.StatusCode.INTERNAL,
        ],
    )
    def test_transient_grpc_codes_are_retryable(self, code):
        assert classify_grpc_error(_rpc_error(code)) == RetryDecision.RETRY

    def test_timeout_error_is_retryable(self):
        assert classify_grpc_error(TimeoutError("deadline")) == RetryDecision.RETRY


class TestClassifyGrpcErrorNoRetry:
    @pytest.mark.parametrize(
        "code",
        [
            grpc.StatusCode.INVALID_ARGUMENT,
            grpc.StatusCode.NOT_FOUND,
            grpc.StatusCode.FAILED_PRECONDITION,
            grpc.StatusCode.UNIMPLEMENTED,
            grpc.StatusCode.PERMISSION_DENIED,
            grpc.StatusCode.UNAUTHENTICATED,
            grpc.StatusCode.OUT_OF_RANGE,
        ],
    )
    def test_permanent_grpc_codes_are_not_retryable(self, code):
        assert classify_grpc_error(_rpc_error(code)) == RetryDecision.NO_RETRY

    def test_unexpected_exception_class_is_fail_safe_no_retry(self):
        assert classify_grpc_error(ValueError("something weird")) == RetryDecision.NO_RETRY

    def test_generic_runtime_error_is_no_retry(self):
        assert classify_grpc_error(RuntimeError("boom")) == RetryDecision.NO_RETRY


class TestErrorCodeFromException:
    def test_timeout_error_maps_to_timeout_code(self):
        assert error_code_from_exception(TimeoutError()) == "TIMEOUT"

    def test_grpc_error_maps_to_status_code_name(self):
        exc = _rpc_error(grpc.StatusCode.UNAVAILABLE)
        assert error_code_from_exception(exc) == "UNAVAILABLE"

    def test_unexpected_exception_maps_to_unknown(self):
        assert error_code_from_exception(ValueError("x")) == "UNKNOWN"


class TestTruncateErrorMessage:
    def test_short_message_is_unchanged(self):
        assert truncate_error_message("short") == "short"

    def test_message_at_exact_limit_is_unchanged(self):
        message = "x" * MAX_ERROR_MESSAGE_LENGTH
        assert truncate_error_message(message) == message

    def test_long_message_is_truncated_to_limit(self):
        message = "x" * (MAX_ERROR_MESSAGE_LENGTH + 100)
        result = truncate_error_message(message)
        assert len(result) == MAX_ERROR_MESSAGE_LENGTH
        assert result == "x" * MAX_ERROR_MESSAGE_LENGTH

    def test_custom_limit_is_respected(self):
        assert truncate_error_message("abcdefgh", limit=3) == "abc"


class TestAnalyzerGrpcClient:
    def test_construction_creates_insecure_channel_and_stub(self):
        with patch("app.integrations.analyzer_client.grpc.aio.insecure_channel") as mock_chan, \
             patch("app.integrations.analyzer_client.analyzer_pb2_grpc.PhotoAnalyzerStub") as mock_stub_cls:
            fake_channel = MagicMock()
            mock_chan.return_value = fake_channel

            client = AnalyzerGrpcClient("analyzer-stub:50051", timeout=5.0)

            mock_chan.assert_called_once_with("analyzer-stub:50051")
            mock_stub_cls.assert_called_once_with(fake_channel)
            assert client._timeout == 5.0

    async def test_analyze_calls_stub_with_timeout_and_request_fields(self):
        with patch("app.integrations.analyzer_client.grpc.aio.insecure_channel"), \
             patch("app.integrations.analyzer_client.analyzer_pb2_grpc.PhotoAnalyzerStub") as mock_stub_cls:
            stub = MagicMock()
            stub.AnalyzePhoto = AsyncMock(return_value="fake-response")
            mock_stub_cls.return_value = stub

            client = AnalyzerGrpcClient("addr:50051", timeout=7.5)
            result = await client.analyze("photo-1", "photos/photo-1/original.jpg")

            assert result == "fake-response"
            stub.AnalyzePhoto.assert_awaited_once()
            call = stub.AnalyzePhoto.call_args
            request = call.args[0]
            assert request.photo_id == "photo-1"
            assert request.object_key == "photos/photo-1/original.jpg"
            assert call.kwargs["timeout"] == 7.5

    async def test_close_closes_the_channel(self):
        with patch("app.integrations.analyzer_client.grpc.aio.insecure_channel") as mock_chan, \
             patch("app.integrations.analyzer_client.analyzer_pb2_grpc.PhotoAnalyzerStub"):
            fake_channel = MagicMock()
            fake_channel.close = AsyncMock()
            mock_chan.return_value = fake_channel

            client = AnalyzerGrpcClient("addr:50051", timeout=1.0)
            await client.close()

            fake_channel.close.assert_awaited_once()
