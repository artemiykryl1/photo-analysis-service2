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


_FAKE_IMAGE_BYTES = b"fake-image-bytes"


class TestAnalyzerStubDeterminism:
    def test_same_object_key_always_yields_the_same_result(self, analyzer_stub_module):
        key = "photos/123e4567-e89b-12d3-a456-426614174000/original.jpg"

        first = analyzer_stub_module._analyze(key, _FAKE_IMAGE_BYTES)
        second = analyzer_stub_module._analyze(key, _FAKE_IMAGE_BYTES)

        assert first.faces_count == second.faces_count
        assert first.is_blurred == second.is_blurred
        assert first.blur_score == second.blur_score
        assert first.perceptual_hash == second.perceptual_hash
        assert first.eyes_closed_count == second.eyes_closed_count
        assert first.dominant_color == second.dominant_color
        assert list(first.tags) == list(second.tags)
        assert first.model_version == second.model_version

    def test_result_is_the_same_regardless_of_image_bytes(self, analyzer_stub_module):
        """TASK-003 D6: determinism is keyed off `object_key`, not the
        actual image content - a stable per-photo result matters more for
        tests/demos than reacting to bytes we don't inspect."""
        key = "photos/123e4567-e89b-12d3-a456-426614174000/original.jpg"

        first = analyzer_stub_module._analyze(key, b"one set of bytes")
        second = analyzer_stub_module._analyze(key, b"a completely different set of bytes!!")

        assert first.blur_score == second.blur_score
        assert first.perceptual_hash == second.perceptual_hash

    def test_different_object_keys_can_yield_different_results(self, analyzer_stub_module):
        results = {
            analyzer_stub_module._analyze(
                f"photos/{i}/original.jpg", _FAKE_IMAGE_BYTES
            ).perceptual_hash
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
        result = analyzer_stub_module._analyze(object_key, _FAKE_IMAGE_BYTES)
        assert 0 <= result.faces_count <= 5

    @pytest.mark.parametrize(
        "object_key",
        ["photos/a/original.jpg", "photos/b/original.png", "photos/c/original.jpeg", ""],
    )
    def test_blur_score_is_within_documented_range(self, analyzer_stub_module, object_key):
        """TASK-003 A16 (design §7/D6): reproduces the real analyzer's
        observed order-of-magnitude range (spike A3: 0.375-99 774), NOT the
        old stub's 0..1 scale."""
        result = analyzer_stub_module._analyze(object_key, _FAKE_IMAGE_BYTES)
        assert 1.0 <= result.blur_score <= 100_000.0

    @pytest.mark.parametrize(
        "object_key",
        ["photos/a/original.jpg", "photos/b/original.png", "photos/c/original.jpeg", ""],
    )
    def test_is_blurred_matches_blur_score_threshold(self, analyzer_stub_module, object_key):
        """TASK-003 design §7: unlike the real analyzer (observed always
        `True`, spike A3 finding 3), the stub computes this field honestly
        - it is simply unused by `select_best_photo` (design §3.4)."""
        result = analyzer_stub_module._analyze(object_key, _FAKE_IMAGE_BYTES)
        assert result.is_blurred == (result.blur_score < 100.0)

    @pytest.mark.parametrize(
        "object_key",
        ["photos/a/original.jpg", "photos/b/original.png", "photos/c/original.jpeg", ""],
    )
    def test_perceptual_hash_is_16_hex_characters(self, analyzer_stub_module, object_key):
        result = analyzer_stub_module._analyze(object_key, _FAKE_IMAGE_BYTES)
        assert len(result.perceptual_hash) == 16
        int(result.perceptual_hash, 16)  # must be valid hex

    @pytest.mark.parametrize(
        "object_key",
        ["photos/a/original.jpg", "photos/b/original.png", "photos/c/original.jpeg", ""],
    )
    def test_eyes_closed_count_never_exceeds_faces_count(self, analyzer_stub_module, object_key):
        result = analyzer_stub_module._analyze(object_key, _FAKE_IMAGE_BYTES)
        assert 0 <= result.eyes_closed_count <= result.faces_count

    @pytest.mark.parametrize(
        "object_key",
        ["photos/a/original.jpg", "photos/b/original.png", "photos/c/original.jpeg", ""],
    )
    def test_dominant_color_is_a_valid_hex_color(self, analyzer_stub_module, object_key):
        result = analyzer_stub_module._analyze(object_key, _FAKE_IMAGE_BYTES)
        assert result.dominant_color.startswith("#")
        assert len(result.dominant_color) == 7
        int(result.dominant_color[1:], 16)  # must be valid hex

    def test_model_version_is_set(self, analyzer_stub_module):
        result = analyzer_stub_module._analyze("photos/a/original.jpg", _FAKE_IMAGE_BYTES)
        assert result.model_version
