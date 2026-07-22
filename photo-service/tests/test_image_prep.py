"""Behavioural coverage of `app.services.image_prep.prepare_for_analysis`
(TASK-003 A5, tasks/TASK-003/20_design.md §2).

Purely a Pillow-fixture test - no MinIO, no gRPC, no Settings beyond the
plain defaults, matching the module's own "no I/O" contract. Images are
generated in-memory (never read from disk) to keep the suite hermetic and
fast; property-based assertions (fits the budget, side within bounds,
JPEG output, repeat call gives identical bytes) are used instead of golden
hashes so an eventual Pillow version bump does not make this suite flaky
(design §2 "Детерминизм").
"""

import io

import pytest
from PIL import Image

from app.core.config import Settings
from app.services.image_prep import (
    ImageDecodeError,
    ImageTooLargeError,
    prepare_for_analysis,
)


def _jpeg_bytes(width: int, height: int, quality: int = 90) -> bytes:
    """A JPEG with actual noise (not a flat color) so it does not
    compress down to a handful of bytes regardless of resolution - large
    dimensions should produce a large file, as a real photo would."""
    image = Image.effect_noise((width, height), 60).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def _png_bytes_rgba(width: int, height: int, alpha: int = 128) -> bytes:
    image = Image.new("RGBA", (width, height), (255, 0, 0, alpha))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestEarlyReturn:
    def test_small_image_within_side_budget_is_returned_unmodified(self, settings):
        data = _jpeg_bytes(200, 150)

        result = prepare_for_analysis(data, settings)

        assert result.data == data
        assert result.downscaled is False
        assert result.width == 200
        assert result.height == 150

    def test_small_rgba_png_within_budget_passes_through_without_flattening(self, settings):
        """The early-return branch (design §2 step 1) sends the original
        bytes as-is, regardless of format/alpha - flattening only happens
        on the downscale ladder path (step 2)."""
        data = _png_bytes_rgba(64, 64)

        result = prepare_for_analysis(data, settings)

        assert result.data == data
        assert result.downscaled is False


class TestDownscaleLadder:
    def test_oversized_image_is_downscaled_to_fit_the_byte_and_side_budget(self, settings):
        data = _jpeg_bytes(3000, 2000, quality=95)
        assert len(data) > settings.ANALYZER_MAX_IMAGE_BYTES  # sanity: input exceeds the budget

        result = prepare_for_analysis(data, settings)

        assert result.downscaled is True
        assert len(result.data) <= settings.ANALYZER_MAX_IMAGE_BYTES
        assert max(result.width, result.height) <= settings.ANALYZER_MAX_IMAGE_SIDE
        # output is always JPEG (design §2 "формат выхода - всегда JPEG")
        assert Image.open(io.BytesIO(result.data)).format == "JPEG"

    def test_oversized_image_over_the_side_budget_but_under_byte_budget_is_still_downscaled(
        self, settings
    ):
        """The early-return condition requires BOTH `len(data) <=
        ANALYZER_MAX_IMAGE_BYTES` AND `max(width, height) <=
        ANALYZER_MAX_IMAGE_SIDE` - a small-in-bytes but very wide/tall
        image must still go through the ladder."""
        image = Image.new("RGB", (4000, 100), (10, 20, 30))  # flat color -> tiny JPEG
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        data = buffer.getvalue()
        assert len(data) <= settings.ANALYZER_MAX_IMAGE_BYTES  # small in bytes
        assert max(image.size) > settings.ANALYZER_MAX_IMAGE_SIDE  # but oversized in side

        result = prepare_for_analysis(data, settings)

        assert result.downscaled is True
        assert max(result.width, result.height) <= settings.ANALYZER_MAX_IMAGE_SIDE

    def test_rgba_png_downscale_path_flattens_alpha_onto_white_background(self, settings):
        data = _png_bytes_rgba(3000, 2200, alpha=100)

        result = prepare_for_analysis(data, settings)

        assert result.downscaled is True
        out = Image.open(io.BytesIO(result.data))
        assert out.mode == "RGB"  # no alpha channel survives

    def test_grayscale_image_downscale_path_is_converted_to_rgb(self, settings):
        """`_to_rgb`'s final branch (`image.mode != "RGB"` -> `.convert("RGB")`)
        handles formats with no alpha channel to flatten but that are still
        not RGB (grayscale `L`, `CMYK`, palette without transparency, ...) -
        the alpha-flattening branch above does not apply to these, so this
        exercises the other conversion path explicitly."""
        image = Image.effect_noise((3000, 2200), 60).convert("L")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        assert Image.open(io.BytesIO(data)).mode == "L"  # sanity: input is grayscale, no alpha

        result = prepare_for_analysis(data, settings)

        assert result.downscaled is True
        out = Image.open(io.BytesIO(result.data))
        assert out.mode == "RGB"

    def test_repeated_calls_on_the_same_input_are_byte_identical(self, settings):
        """Determinism (design §2): same input + same Pillow version +
        fixed ladder -> identical output bytes, not just "close enough"."""
        data = _jpeg_bytes(3000, 2000, quality=95)

        first = prepare_for_analysis(data, settings)
        second = prepare_for_analysis(data, settings)

        assert first.data == second.data
        assert first.downscaled == second.downscaled
        assert (first.width, first.height) == (second.width, second.height)


class TestImageTooLargeError:
    def test_pixel_count_over_the_limit_is_rejected_without_decoding(self, settings):
        """Decompression-bomb guard (design §2): rejected purely from the
        header dimensions - a large `L`-mode canvas keeps this test's own
        memory footprint modest while still exceeding
        ANALYZER_MAX_IMAGE_PIXELS."""
        width, height = 8000, 7000
        assert width * height > settings.ANALYZER_MAX_IMAGE_PIXELS
        image = Image.new("L", (width, height), color=128)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()

        with pytest.raises(ImageTooLargeError):
            prepare_for_analysis(data, settings)

    def test_no_downscale_step_fitting_raises_image_too_large(self, settings, monkeypatch):
        """Even the smallest ladder step (800px @ q60) is practically
        always well under budget for a real photo - force the failure
        deterministically by shrinking the byte budget to an unreachable
        value rather than relying on a pathological fixture image."""
        data = _jpeg_bytes(3000, 2000, quality=95)
        tiny_budget_settings = settings.model_copy(update={"ANALYZER_MAX_IMAGE_BYTES": 10})

        with pytest.raises(ImageTooLargeError):
            prepare_for_analysis(data, tiny_budget_settings)


class TestImageDecodeError:
    def test_non_image_bytes_raise_image_decode_error(self, settings):
        with pytest.raises(ImageDecodeError):
            prepare_for_analysis(b"this is not an image, just text", settings)

    def test_empty_bytes_raise_image_decode_error(self, settings):
        with pytest.raises(ImageDecodeError):
            prepare_for_analysis(b"", settings)

    def test_truncated_small_jpeg_passes_through_unmodified(self, settings):
        """A truncated file that is still small enough for the early-return
        branch (design §2 step 1) is never actually decoded - only its
        header (width/height) is read - so truncation of the entropy-coded
        body goes undetected here. This mirrors real Pillow behavior, not
        a gap: nothing downstream needs full pixel data unless we are
        about to resize/re-encode it."""
        data = _jpeg_bytes(200, 150)
        truncated = data[: len(data) // 2]

        result = prepare_for_analysis(truncated, settings)

        assert result.downscaled is False
        assert result.data == truncated

    def test_truncated_oversized_jpeg_raises_image_decode_error(self, settings):
        """A truncated file that must go through the downscale ladder DOES
        get fully decoded (`_encode_step`'s `thumbnail()`/`load()`) - a
        truncated entropy stream must surface as `ImageDecodeError`, not a
        raw Pillow `OSError` that the rest of the pipeline does not know
        how to classify."""
        data = _jpeg_bytes(3000, 2000, quality=95)
        truncated = data[: len(data) // 2]

        with pytest.raises(ImageDecodeError):
            prepare_for_analysis(truncated, settings)
