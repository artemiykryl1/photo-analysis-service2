"""Behavioural coverage of `analyzer-stub/server.py`'s deterministic result
generator (design §6, §13.2 "Детерминизм analyzer-stub").

`analyzer-stub/` is deliberately standalone (does not import `app.*`, see
its module docstring - it simulates an external service per
constitution.md §2.1) and its generated protobuf stubs
(`analyzer_pb2`/`analyzer_pb2_grpc`) are produced at Docker build time, not
committed there. To exercise the real `_analyze()` function without
duplicating its logic in the test, this module loads `server.py` directly
via `importlib` with `sys.path` temporarily extended to the already-committed
`app/grpc_gen/` stubs (same wire contract, `protos/analyzer.proto` is
unchanged between the two - design §7.1) so the flat `import analyzer_pb2`/
`import analyzer_pb2_grpc` in `server.py` resolve correctly in this test
process. This never starts a gRPC server and never talks to a real
analyzer - only the pure `_analyze(object_key)` function is called.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

PHOTO_SERVICE_ROOT = Path(__file__).resolve().parent.parent
GRPC_GEN_DIR = PHOTO_SERVICE_ROOT / "app" / "grpc_gen"
ANALYZER_STUB_DIR = PHOTO_SERVICE_ROOT / "analyzer-stub"


def _load_analyzer_stub_module():
    """Import `analyzer-stub/server.py` as a standalone module, with
    `app/grpc_gen/` (committed stabs) and `analyzer-stub/` added to
    `sys.path` so its flat `import analyzer_pb2`/`analyzer_pb2_grpc`
    succeed exactly as they would in the built Docker image (where those
    modules are generated as siblings of `server.py`)."""
    added_paths = []
    for path in (str(GRPC_GEN_DIR), str(ANALYZER_STUB_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)
            added_paths.append(path)

    try:
        # Ensure the flat modules resolve from app/grpc_gen, not any stale
        # `analyzer_pb2` that might already be registered under a different
        # identity (defensive - keeps this test order-independent).
        sys.modules.pop("analyzer_pb2", None)
        sys.modules.pop("analyzer_pb2_grpc", None)

        spec = importlib.util.spec_from_file_location(
            "analyzer_stub_server_under_test", ANALYZER_STUB_DIR / "server.py"
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


class TestAnalyzerStubDeterminism:
    def test_same_object_key_always_yields_the_same_result(self, analyzer_stub_module):
        key = "photos/123e4567-e89b-12d3-a456-426614174000/original.jpg"

        first = analyzer_stub_module._analyze(key)
        second = analyzer_stub_module._analyze(key)

        assert first.faces_count == second.faces_count
        assert first.is_blurred == second.is_blurred
        assert first.blur_score == second.blur_score
        assert first.perceptual_hash == second.perceptual_hash

    def test_different_object_keys_can_yield_different_results(self, analyzer_stub_module):
        results = {
            analyzer_stub_module._analyze(f"photos/{i}/original.jpg").perceptual_hash
            for i in range(10)
        }

        assert len(results) > 1  # not a constant function

    @pytest.mark.parametrize(
        "object_key",
        [
            "photos/a/original.jpg",
            "photos/b/original.png",
            "photos/c/original.jpeg",
            "",
            "photos/" + "x" * 200 + "/original.jpg",
        ],
    )
    def test_faces_count_is_within_documented_range(self, analyzer_stub_module, object_key):
        result = analyzer_stub_module._analyze(object_key)
        assert 0 <= result.faces_count <= 5

    @pytest.mark.parametrize(
        "object_key",
        ["photos/a/original.jpg", "photos/b/original.png", "photos/c/original.jpeg", ""],
    )
    def test_blur_score_is_within_documented_range(self, analyzer_stub_module, object_key):
        result = analyzer_stub_module._analyze(object_key)
        assert 0.0 <= result.blur_score <= 1.0

    @pytest.mark.parametrize(
        "object_key",
        ["photos/a/original.jpg", "photos/b/original.png", "photos/c/original.jpeg", ""],
    )
    def test_is_blurred_matches_blur_score_threshold(self, analyzer_stub_module, object_key):
        result = analyzer_stub_module._analyze(object_key)
        assert result.is_blurred == (result.blur_score > 0.6)

    @pytest.mark.parametrize(
        "object_key",
        ["photos/a/original.jpg", "photos/b/original.png", "photos/c/original.jpeg", ""],
    )
    def test_perceptual_hash_is_16_hex_characters(self, analyzer_stub_module, object_key):
        result = analyzer_stub_module._analyze(object_key)
        assert len(result.perceptual_hash) == 16
        int(result.perceptual_hash, 16)  # must be valid hex
