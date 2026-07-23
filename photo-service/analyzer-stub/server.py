"""Minimal gRPC server stub for `PhotoAnalyzer` (tasks/TASK-002/20_design.md §6).

Deliberately standalone - does NOT import anything from `app.*`. This
simulates an external service (constitution.md §2.1: "Разработка моделей
анализа фотографий... берутся как external service"). Replacing this
container with a real analyzer in TASK-003 is just pointing
`ANALYZER_GRPC_ADDR` at a different image implementing the same
`protos/analyzer.proto` contract.

`analyzer_pb2`/`analyzer_pb2_grpc` are generated at Docker build time from
`protos/analyzer.proto` into this same directory (see Dockerfile), so the
flat imports below (not a package import) are correct for this container.

Results are a deterministic function of `object_key` (sha256-derived), so
the same photo always analyzes to the same result - this is what makes
the pipeline reproducible in tests/demos (design §6):

    faces_count      = sha256(object_key)[0] % 6
    blur_score       = (int(sha256(object_key)[1:3]) % 1001) / 1000.0
    is_blurred        = blur_score > 0.6
    perceptual_hash   = sha256(object_key).hex()[:16]
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


def _analyze(object_key: str) -> "analyzer_pb2.AnalyzePhotoResponse":
    digest = hashlib.sha256(object_key.encode("utf-8")).digest()
    faces_count = digest[0] % 6
    blur_score = (int.from_bytes(digest[1:3], "big") % 1001) / 1000.0
    is_blurred = blur_score > 0.6
    perceptual_hash = digest.hex()[:16]
    return analyzer_pb2.AnalyzePhotoResponse(
        faces_count=faces_count,
        is_blurred=is_blurred,
        blur_score=blur_score,
        perceptual_hash=perceptual_hash,
    )


class PhotoAnalyzerServicer(analyzer_pb2_grpc.PhotoAnalyzerServicer):
    async def AnalyzePhoto(self, request, context):
        response = _analyze(request.object_key)
        logger.info(
            "analyzed photo_id=%s faces_count=%d is_blurred=%s blur_score=%.3f",
            request.photo_id,
            response.faces_count,
            response.is_blurred,
            response.blur_score,
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
