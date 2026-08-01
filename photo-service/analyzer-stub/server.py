"""Minimal gRPC server stub for `PhotoAnalyzer` (tasks/TASK-002/20_design.md §6,
updated for the real contract by TASK-003 A16, tasks/TASK-003/20_design.md §7/D6).

Deliberately standalone - does NOT import anything from `app.*`. This
simulates an external service (constitution.md §2.1: "Разработка моделей
анализа фотографий... берутся как external service"). This container
remains the DEFAULT analyzer for local `docker-compose.yml` and for the
whole test suite (design §1, "Что НЕ трогать" #9) - the real analyzer is
only ever used by pointing `ANALYZER_GRPC_ADDR` at it
(`docker-compose.server.yml`), never by changing this stub's behavior.

`analyzer_pb2`/`analyzer_pb2_grpc` are generated at Docker build time from
`protos/analyzer.proto` into this same directory (see Dockerfile), so the
flat imports below (not a package import) are correct for this container.

Results are a deterministic function of `object_key` (sha256-derived, NOT
`image_bytes`) so the same photo always analyzes to the same result - this
is what makes the pipeline reproducible in tests/demos (design §6). Basing
determinism on `object_key` rather than the actual bytes is a deliberate
choice (design D6): tests/demos need a stable result per photo, and
fixturing specific byte sequences is far more expensive than keying off a
string that is already unique per photo. `image_bytes` is still required
and validated (empty -> `INVALID_ARGUMENT`) purely as a regression guard -
if a caller forgets to attach the prepared bytes, the stub notices instead
of silently "succeeding" on a payload the real analyzer would reject.

TASK-003 A16/spike A3 finding 3 (tasks/TASK-003/05_spike_analyzer.md): the
real analyzer's `blur_score` is a Laplacian variance where a HIGHER value
means SHARPER, ranging unbounded (spike measured 0.4-2.9 for blurry frames,
930-99 774 for sharp ones) - NOT the old stub's 0..1 "lower is sharper"
scale. `blur_score` here is generated on a log scale spanning roughly
1.0-100 000 to reproduce that same "orders of magnitude" spread, still
deterministic from `object_key`.

The real analyzer's `is_blurred` was observed to be `True` in every single
spike measurement, including the sharpest sample (design finding 3) - it
looks like a bug on the real service's side. This stub deliberately does
NOT reproduce that defect: `is_blurred` here is computed correctly
(`blur_score < SHARPNESS_THRESHOLD`-equivalent), because
`app.services.batch_service.select_best_photo` no longer reads
`is_blurred` at all (design §3.4) - there is nothing for a "faithfully
broken" `is_blurred` to protect against here, and a stub that quietly
copied a defect would be a worse test double than one that just computes
the field honestly.
"""

import asyncio
import hashlib
import logging
import os

import grpc

import analyzer_pb2
import analyzer_pb2_grpc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("analyzer-stub")

GRPC_PORT = int(os.environ.get("ANALYZER_GRPC_PORT", "50051"))

# Matches app.services.batch_service.SHARPNESS_THRESHOLD (design §7: "Заглушка
# считает поле правильно"). Not imported (this container has no dependency
# on `app.*`) - kept in sync manually, as documented here and there.
_SHARPNESS_THRESHOLD = 100.0

_MODEL_VERSION = "stub/2.0.0"


def _analyze(object_key: str, image_bytes: bytes) -> "analyzer_pb2.AnalyzePhotoResponse":
    digest = hashlib.sha256(object_key.encode("utf-8")).digest()

    faces_count = digest[0] % 6
    # Log-scale 1.0 .. 100_000, reproducing the real analyzer's observed
    # range/shape (spike A3 finding 3) while staying a deterministic
    # function of `object_key`.
    blur_score = round(10 ** (digest[1] / 255 * 5), 3)
    is_blurred = blur_score < _SHARPNESS_THRESHOLD
    perceptual_hash = digest.hex()[:16]
    eyes_closed_count = digest[4] % (faces_count + 1)  # never more than faces_count
    dominant_color = "#" + digest[5:8].hex()

    tags = ["face" if faces_count > 0 else "no_face", "bright" if digest[2] % 2 else "dark"]
    if is_blurred:
        tags.append("blurry")
    tags.append(f"dominant:{dominant_color}")

    return analyzer_pb2.AnalyzePhotoResponse(
        faces_count=faces_count,
        is_blurred=is_blurred,
        blur_score=blur_score,
        perceptual_hash=perceptual_hash,
        eyes_closed_count=eyes_closed_count,
        dominant_color=dominant_color,
        tags=tags,
        model_version=_MODEL_VERSION,
    )


class PhotoAnalyzerServicer(analyzer_pb2_grpc.PhotoAnalyzerServicer):
    async def AnalyzePhoto(self, request, context):
        if not request.image_bytes:
            # Regression guard (design D6): a caller that forgot to attach
            # the prepared image bytes should fail loudly here, in dev/CI,
            # rather than "succeed" against a real analyzer that would
            # reject an empty payload differently.
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "image_bytes must not be empty"
            )

        response = _analyze(request.object_key, request.image_bytes)
        logger.info(
            "analyzed photo_id=%s faces_count=%d is_blurred=%s blur_score=%.3f "
            "image_bytes_len=%d",
            request.photo_id,
            response.faces_count,
            response.is_blurred,
            response.blur_score,
            len(request.image_bytes),
        )
        return response


async def serve() -> None:
    server = grpc.aio.server()
    analyzer_pb2_grpc.add_PhotoAnalyzerServicer_to_server(PhotoAnalyzerServicer(), server)
    server.add_insecure_port(f"[::]:{GRPC_PORT}")
    await server.start()
    logger.info("analyzer-stub listening on :%d", GRPC_PORT)
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())
