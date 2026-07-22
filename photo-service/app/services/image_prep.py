"""Prepare an uploaded image for the real analyzer's 4 MiB gRPC message cap.

TASK-003 D1 (tasks/TASK-003/20_design.md §2, tasks/TASK-003/05_spike_analyzer.md
finding 1): the real analyzer at `45.132.19.101:50051` hard-caps its gRPC
message at exactly 4 194 304 bytes (the gRPC default, not raised on the
server). Our public upload limit stays at 50 MB (constitution.md §2.3) - we
do NOT shrink what users can upload; instead the worker prepares a smaller
copy just for the analyzer call:

    1. if the original already fits the analyzer's byte/pixel budget ->
       send it unmodified (`downscaled=False`);
    2. otherwise, downscale via a fixed ladder of (side, JPEG quality)
       steps, picking the first step whose encoded output fits the budget;
    3. if no step fits (practically unreachable - an 800px JPEG at q60 is
       tens of KB) -> raise `ImageTooLargeError`, no analyzer call is made.

This module is deliberately **pure and I/O-free**: no imports from
`app.integrations`, `app.repositories`, `app.db`, no network, no logging.
That purity is what makes it independently unit-testable on Pillow-generated
fixtures (design §2) and safe to run off the event loop via
`anyio.to_thread.run_sync` from `AnalysisProcessor` (decode/resize/encode is
CPU-bound work that must never block asyncio).
"""

from dataclasses import dataclass
from io import BytesIO

from PIL import Image

from app.core.config import Settings

# (max side px, JPEG quality) ladder, tried in order - the first step whose
# encoded output fits `settings.ANALYZER_MAX_IMAGE_BYTES` is used (design §2).
_DOWNSCALE_LADDER: tuple[tuple[int, int], ...] = (
    (2048, 85),
    (1600, 80),
    (1280, 75),
    (1024, 70),
    (800, 60),
)


class ImageDecodeError(Exception):
    """The input bytes could not be decoded as an image by Pillow."""


class ImageTooLargeError(Exception):
    """The image exceeds `ANALYZER_MAX_IMAGE_PIXELS`, or no downscale ladder
    step fit `ANALYZER_MAX_IMAGE_BYTES` - a permanent (NO_RETRY) condition,
    never a transient one (design §4.2)."""


@dataclass(frozen=True)
class PreparedImage:
    """Result of `prepare_for_analysis` - the bytes to send to the analyzer
    plus metadata for logging/metrics (design §11.2)."""

    data: bytes
    downscaled: bool
    width: int
    height: int


def _open(data: bytes) -> Image.Image:
    try:
        image = Image.open(BytesIO(data))
        # Force a header parse now (Image.open is lazy) so a truncated/
        # corrupt file surfaces as ImageDecodeError here, not later inside
        # draft()/load() deep in the downscale loop.
        image.verify()
    except Exception as exc:  # noqa: BLE001 - Pillow raises many exception types
        raise ImageDecodeError(f"could not decode image: {exc}") from exc
    # `verify()` leaves the file object unusable for further ops - reopen.
    return Image.open(BytesIO(data))


def _to_rgb(image: Image.Image) -> Image.Image:
    """Flatten any alpha channel onto a white background and return an RGB
    image (design §2: "формат выхода - всегда JPEG, RGB")."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _encode_step(data: bytes, side: int, quality: int) -> bytes:
    """Decode `data` fresh, downscale to fit within `side`x`side`, and
    re-encode as JPEG at `quality`. A fresh decode per step (rather than
    reusing a mutated in-memory image) keeps each ladder step an
    independent, side-effect-free operation."""
    image = Image.open(BytesIO(data))
    if image.format == "JPEG":
        # `draft()` lets libjpeg downscale during DCT decoding itself,
        # which is dramatically cheaper in both memory and CPU than
        # decoding at full resolution and resizing afterwards (design §2,
        # "экономя до 16x памяти и времени"). It must be called before any
        # `load()`/`resize()` and is best-effort (returns None if the
        # format/mode combination cannot be drafted).
        image.draft("RGB", (side, side))
    image = _to_rgb(image)
    image.thumbnail((side, side), Image.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def prepare_for_analysis(data: bytes, settings: Settings) -> PreparedImage:
    """Return the bytes to send as `AnalyzePhotoRequest.image_bytes`.

    Raises `ImageDecodeError` if `data` is not a decodable image, and
    `ImageTooLargeError` if the image exceeds `ANALYZER_MAX_IMAGE_PIXELS` or
    no downscale ladder step fits `ANALYZER_MAX_IMAGE_BYTES`.
    """
    image = _open(data)
    width, height = image.size

    if width * height > settings.ANALYZER_MAX_IMAGE_PIXELS:
        # Decompression-bomb guard (design §2): rejected purely from the
        # header - `Image.open` is lazy and never raised a full-resolution
        # raster for this check.
        raise ImageTooLargeError(
            f"image is {width}x{height} ({width * height} px), "
            f"exceeds ANALYZER_MAX_IMAGE_PIXELS={settings.ANALYZER_MAX_IMAGE_PIXELS}"
        )

    if len(data) <= settings.ANALYZER_MAX_IMAGE_BYTES and max(width, height) <= (
        settings.ANALYZER_MAX_IMAGE_SIDE
    ):
        return PreparedImage(data=data, downscaled=False, width=width, height=height)

    for side, quality in _DOWNSCALE_LADDER:
        try:
            encoded = _encode_step(data, side, quality)
        except Exception as exc:  # noqa: BLE001 - Pillow raises many exception types
            # A file can pass the cheap header-only `_open()`/`verify()`
            # check above (structure looks sane) yet still fail during a
            # REAL decode - e.g. a JPEG truncated mid-entropy-stream, which
            # `verify()` does not always catch. Without this, such a file
            # would raise a raw Pillow `OSError` here instead of the
            # documented `ImageDecodeError` the rest of the pipeline
            # classifies on (`app.services.analysis_errors`).
            raise ImageDecodeError(
                f"could not decode image during downscale (side={side}): {exc}"
            ) from exc
        if len(encoded) <= settings.ANALYZER_MAX_IMAGE_BYTES:
            out = Image.open(BytesIO(encoded))
            return PreparedImage(
                data=encoded, downscaled=True, width=out.width, height=out.height
            )

    raise ImageTooLargeError(
        f"image {width}x{height} ({len(data)} bytes) does not fit "
        f"ANALYZER_MAX_IMAGE_BYTES={settings.ANALYZER_MAX_IMAGE_BYTES} "
        "even at the smallest downscale ladder step"
    )
