"""Real (in-process, loopback-only) gRPC round-trip against
`analyzer-stub/server.py`'s `PhotoAnalyzerServicer` (TASK-003 A16, design
§7/D6).

Every other analyzer-stub test (`test_analyzer_stub.py`) calls the pure
`_analyze()` function directly and never touches gRPC at all; every
`AnalyzerGrpcClient`/`AnalysisProcessor` test mocks the analyzer entirely.
Neither exercises the servicer's `AnalyzePhoto` RPC handler itself -
notably its `image_bytes` emptiness guard (`context.abort(INVALID_ARGUMENT,
...)`), which 30_impl.md/review-2 flagged as implemented but not covered by
an automated test.

This module starts a REAL `grpc.aio.server()` bound to `127.0.0.1:0` (an
ephemeral local port chosen by the OS - never a fixed port, so this is safe
to run in parallel/CI) and drives it with the real, generated
`PhotoAnalyzerStub` via `app.integrations.analyzer_client.AnalyzerGrpcClient`
- the same client class the worker uses. No network access beyond loopback,
no dependency on the external analyzer at 45.132.19.101.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import grpc
import pytest

from app.integrations.analyzer_client import AnalyzerGrpcClient

PHOTO_SERVICE_ROOT = Path(__file__).resolve().parent.parent
GRPC_GEN_DIR = PHOTO_SERVICE_ROOT / "app" / "grpc_gen"
ANALYZER_STUB_DIR = PHOTO_SERVICE_ROOT / "analyzer-stub"


def _load_analyzer_stub_module():
    """Same loading strategy as `test_analyzer_stub.py`: import
    `analyzer-stub/server.py` standalone, with `app/grpc_gen/` and
    `analyzer-stub/` temporarily on `sys.path` so its flat
    `import analyzer_pb2`/`analyzer_pb2_grpc` resolve exactly as they do in
    the built Docker image."""
    added_paths = []
    for path in (str(GRPC_GEN_DIR), str(ANALYZER_STUB_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)
            added_paths.append(path)

    try:
        sys.modules.pop("analyzer_pb2", None)
        sys.modules.pop("analyzer_pb2_grpc", None)

        spec = importlib.util.spec_from_file_location(
            "analyzer_stub_server_grpc_under_test", ANALYZER_STUB_DIR / "server.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for path in added_paths:
            if path in sys.path:
                sys.path.remove(path)


@pytest.fixture(scope="module")
def analyzer_stub_module():
    if not (ANALYZER_STUB_DIR / "server.py").exists():
        pytest.skip("analyzer-stub/server.py not found")
    if not (GRPC_GEN_DIR / "analyzer_pb2.py").exists():
        pytest.skip("app/grpc_gen stubs not found - run the codegen command first")
    return _load_analyzer_stub_module()


@pytest.fixture
async def running_stub_server(analyzer_stub_module):
    """Start a real `grpc.aio.server()` on an OS-assigned loopback port,
    serving `analyzer_stub_module.PhotoAnalyzerServicer`; yield its address
    and tear it down afterwards."""
    server = grpc.aio.server()
    analyzer_stub_module.analyzer_pb2_grpc.add_PhotoAnalyzerServicer_to_server(
        analyzer_stub_module.PhotoAnalyzerServicer(), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(grace=None)


class TestRealGrpcRoundTrip:
    async def test_analyze_photo_returns_all_eight_fields_over_a_real_channel(
        self, running_stub_server
    ):
        client = AnalyzerGrpcClient(running_stub_server, timeout=5.0)
        try:
            response = await client.analyze(
                "photo-1", "photos/x/original.jpg", b"fake-prepared-image-bytes"
            )
        finally:
            await client.close()

        assert response.perceptual_hash
        assert response.model_version
        assert 0 <= response.faces_count <= 5
        assert isinstance(response.is_blurred, bool)
        assert response.blur_score >= 1.0
        assert 0 <= response.eyes_closed_count <= response.faces_count
        assert response.dominant_color.startswith("#")
        assert len(list(response.tags)) > 0

    async def test_analyze_photo_is_deterministic_across_two_real_calls(
        self, running_stub_server
    ):
        client = AnalyzerGrpcClient(running_stub_server, timeout=5.0)
        try:
            first = await client.analyze("p1", "photos/same-key/original.jpg", b"bytes-a")
            second = await client.analyze("p2", "photos/same-key/original.jpg", b"bytes-b")
        finally:
            await client.close()

        assert first.blur_score == second.blur_score
        assert first.perceptual_hash == second.perceptual_hash

    async def test_empty_image_bytes_is_rejected_with_invalid_argument(
        self, running_stub_server
    ):
        """Design D6 regression guard: a caller that forgot to attach the
        prepared bytes must fail loudly (`INVALID_ARGUMENT`) instead of the
        stub silently "succeeding" on a payload the real analyzer would
        reject. Flagged as implemented-but-untested in 30_impl.md/41_review-2.md
        - closed here with a real gRPC call, not a servicer-internal check."""
        client = AnalyzerGrpcClient(running_stub_server, timeout=5.0)
        try:
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await client.analyze("photo-1", "photos/x/original.jpg", b"")
        finally:
            await client.close()

        assert exc_info.value.code() is grpc.StatusCode.INVALID_ARGUMENT

    async def test_two_concurrent_calls_do_not_interfere(self, running_stub_server):
        """Sanity check that the async servicer handles concurrent RPCs
        correctly (no shared mutable state between calls)."""
        client = AnalyzerGrpcClient(running_stub_server, timeout=5.0)
        try:
            first, second = await asyncio.gather(
                client.analyze("p1", "photos/aaa/original.jpg", b"x"),
                client.analyze("p2", "photos/bbb/original.jpg", b"y"),
            )
        finally:
            await client.close()

        # Different object_keys are extremely likely (deterministic sha256
        # digest) to produce a different perceptual_hash - a cheap signal
        # that the two calls were not accidentally conflated.
        assert first.perceptual_hash != second.perceptual_hash
