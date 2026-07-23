"""Business logic layer for the photos domain.

`PhotoService` orchestrates `repositories/` (DB access) and
`integrations/` (MinIO, and later Kafka). It must not know about FastAPI
(`Request`/`Depends`) - callers (api/) prepare all arguments, including an
already-open `AsyncSession`, and pass raw `bytes` rather than an
`UploadFile`.

TASK-002 (tasks/TASK-002/20_design.md §4.1, §12 step 13): `create_photo`
now stamps the simplified outbox columns (`publish_status='not_sent'`,
`trace_id`) in the same transaction as the insert - it never talks to
Kafka itself, that is exclusively the job of the background outbox
publisher (`app.services.outbox`). `get_photo`/`list_photos` map the
eager-loaded `Photo.analysis` relationship into the response's nullable
`analysis` field.

TASK-002 block B (design §12 step 21): `create_batch` reuses the exact
same per-file validation (`_validate`) as `create_photo`, but runs it for
every file BEFORE any DB/MinIO write - so an invalid file anywhere in the
batch rejects the whole batch atomically (nothing saved, design §11 risk
#6). `PhotoService` gains an optional `batch_repository` collaborator for
this; it defaults to a fresh `BatchRepository()` so existing callers/tests
that only pass `repository=`/`storage=` are unaffected.
"""

import asyncio
import logging
import uuid

import anyio.to_thread
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    BatchSizeError,
    NotFoundError,
    PayloadTooLargeError,
    StorageUnavailable,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.core.logging import trace_id_var
from app.db.models import AnalysisResult, Batch, Photo, PhotoStatus
from app.integrations.metrics import photos_uploaded_total, storage_upload_errors_total
from app.integrations.storage import ObjectStorage
from app.repositories.batch_repository import BatchRepository
from app.repositories.photo_repository import PhotoRepository
from app.schemas.photos import (
    AnalysisResultResponse,
    BatchAcceptedResponse,
    BatchPhotoItem,
    PhotoResponse,
    UploadPhotoResponse,
)

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB, constitution.md §2.3/§3.3
STORAGE_TIMEOUT_SECONDS = 60  # constitution.md §3.2

MIN_BATCH_SIZE = 2  # feature-upload/tasks.md TASK-002 "Зафиксированные решения"
MAX_BATCH_SIZE = 10

# Review-2 fix (tasks/TASK-002/41_review-2.md #6): each file is already
# capped at MAX_FILE_SIZE_BYTES individually, but nothing stopped a batch of
# up to MAX_BATCH_SIZE files from holding all of them in memory at once
# (up to 10 x 50MB = 500MB per request). Cap the summed size explicitly.
BATCH_MAX_TOTAL_BYTES = MAX_BATCH_SIZE * MAX_FILE_SIZE_BYTES  # 500 MB

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_EXT_TO_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
}


def _analysis_response(analysis: AnalysisResult | None) -> AnalysisResultResponse | None:
    """Map the ORM `AnalysisResult` (if any) to its Pydantic response
    shape. `None` until the photo reaches `status=done` (design §3.3)."""
    if analysis is None:
        return None
    return AnalysisResultResponse(
        faces_count=analysis.faces_count,
        is_blurred=analysis.is_blurred,
        blur_score=analysis.blur_score,
        perceptual_hash=analysis.perceptual_hash,
    )


class PhotoService:
    def __init__(
        self,
        repository: PhotoRepository,
        storage: ObjectStorage,
        batch_repository: BatchRepository | None = None,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._batches = batch_repository or BatchRepository()

    def _validate(self, data: bytes) -> tuple[str, str]:
        """Validate file contents by magic bytes. Returns (ext, content_type).

        Order matters: empty -> 400, too large -> 413, unrecognized
        signature -> 415 (constitution.md §3.3: MIME by magic bytes only,
        never by filename extension / Content-Type header).
        """
        if len(data) == 0:
            raise ValidationError("Uploaded file is empty")
        if len(data) > MAX_FILE_SIZE_BYTES:
            raise PayloadTooLargeError("File exceeds the maximum allowed size (50 MB)")
        if data[:3] == _JPEG_MAGIC:
            return "jpg", "image/jpeg"
        if data[:8] == _PNG_MAGIC:
            return "png", "image/png"
        raise UnsupportedMediaTypeError("Only JPEG/PNG images are supported")

    @staticmethod
    def _ext_to_mime(object_key: str) -> str:
        ext = object_key.rsplit(".", 1)[-1].lower()
        return _EXT_TO_MIME.get(ext, "application/octet-stream")

    async def create_photo(
        self, session: AsyncSession, filename: str | None, data: bytes
    ) -> UploadPhotoResponse:
        """Validate, persist metadata (add+flush), store bytes in MinIO, commit.

        Order: DB flush -> MinIO save -> commit. On MinIO failure/timeout,
        the DB transaction is rolled back (no orphan row pointing at a
        missing object) and a 503 is raised - see
        tasks/TASK-001/20_design.md §8.

        The photo row is stamped `publish_status='not_sent'` and
        `trace_id=<current request id>` in the same commit - the
        background outbox publisher (`app.services.outbox`) picks it up
        from there and is the only code path that talks to Kafka
        (design §4.1).
        """
        ext, content_type = self._validate(data)

        photo_id = uuid.uuid4()
        object_key = f"photos/{photo_id}/original.{ext}"

        photo = Photo(
            photo_id=photo_id,
            filename=filename or f"{photo_id}.{ext}",
            object_key=object_key,
            status=PhotoStatus.pending,
            publish_status="not_sent",
            trace_id=trace_id_var.get(),
        )

        await self._repository.create(session, photo)
        logger.info(
            "upload received", extra={"photo_id": str(photo_id), "size": len(data)}
        )

        try:
            async with asyncio.timeout(STORAGE_TIMEOUT_SECONDS):
                await anyio.to_thread.run_sync(
                    self._storage.save_file, object_key, data, len(data), content_type
                )
        except (StorageUnavailable, TimeoutError) as exc:
            await session.rollback()
            storage_upload_errors_total.inc()
            logger.warning(
                "MinIO unavailable on upload, rolled back",
                extra={"photo_id": str(photo_id)},
            )
            raise StorageUnavailable("Storage is unreachable", photo_id=str(photo_id)) from exc

        logger.info(
            "stored in MinIO", extra={"photo_id": str(photo_id), "size": len(data)}
        )

        await session.commit()
        photos_uploaded_total.inc()
        logger.info("photo row committed", extra={"photo_id": str(photo_id)})

        return UploadPhotoResponse(photo_id=str(photo_id), status="pending")

    async def create_batch(
        self, session: AsyncSession, files: list[tuple[str | None, bytes]]
    ) -> BatchAcceptedResponse:
        """Validate 2-10 files, then persist a `Batch` + one `Photo` per
        file atomically (design §12 step 21, §11 risk #6).

        Two-phase: (1) validate every file's bytes with the same
        `_validate` used by `create_photo` - BEFORE any DB/MinIO write, so
        a single invalid file rejects the whole batch with nothing saved,
        AND check the summed size of all files against `BATCH_MAX_TOTAL_BYTES`
        (413 if exceeded - review-2 fix #6); (2) only then create the
        batch/photo rows and store each file in MinIO. A MinIO failure
        partway through rolls back the (uncommitted) transaction and
        best-effort deletes any objects already saved for this batch,
        mirroring `create_photo`'s single-upload contract.
        """
        if not (MIN_BATCH_SIZE <= len(files) <= MAX_BATCH_SIZE):
            raise BatchSizeError(
                f"Batch must contain between {MIN_BATCH_SIZE} and {MAX_BATCH_SIZE} files "
                f"(got {len(files)})"
            )

        # Phase 1: validate every file first - no DB/MinIO writes yet.
        validated = [(filename, data, *self._validate(data)) for filename, data in files]

        total_size = sum(len(data) for _, data, _, _ in validated)
        if total_size > BATCH_MAX_TOTAL_BYTES:
            raise PayloadTooLargeError(
                f"Batch total size {total_size} bytes exceeds the maximum allowed "
                f"{BATCH_MAX_TOTAL_BYTES} bytes"
            )

        batch = Batch(batch_id=uuid.uuid4(), status="processing")
        await self._batches.create(session, batch)

        trace_id = trace_id_var.get()
        photos: list[Photo] = []
        saved_object_keys: list[str] = []

        try:
            for filename, data, ext, content_type in validated:
                photo_id = uuid.uuid4()
                object_key = f"photos/{photo_id}/original.{ext}"
                photo = Photo(
                    photo_id=photo_id,
                    filename=filename or f"{photo_id}.{ext}",
                    object_key=object_key,
                    status=PhotoStatus.pending,
                    publish_status="not_sent",
                    trace_id=trace_id,
                    batch_id=batch.batch_id,
                )
                await self._repository.create(session, photo)

                async with asyncio.timeout(STORAGE_TIMEOUT_SECONDS):
                    await anyio.to_thread.run_sync(
                        self._storage.save_file, object_key, data, len(data), content_type
                    )
                saved_object_keys.append(object_key)
                photos.append(photo)
        except (StorageUnavailable, TimeoutError) as exc:
            await session.rollback()
            storage_upload_errors_total.inc()
            logger.warning(
                "MinIO unavailable during batch upload, rolled back",
                extra={"batch_id": str(batch.batch_id)},
            )
            for object_key in saved_object_keys:
                await anyio.to_thread.run_sync(self._storage.delete_file, object_key)
            raise StorageUnavailable("Storage is unreachable") from exc

        await session.commit()
        photos_uploaded_total.inc(len(photos))
        logger.info(
            "batch committed",
            extra={"batch_id": str(batch.batch_id), "photo_count": len(photos)},
        )

        return BatchAcceptedResponse(
            batch_id=str(batch.batch_id),
            photos=[
                BatchPhotoItem(photo_id=str(p.photo_id), status="pending") for p in photos
            ],
        )

    async def get_photo(self, session: AsyncSession, photo_id: uuid.UUID) -> PhotoResponse:
        photo = await self._repository.get_by_id(session, photo_id)
        if photo is None:
            raise NotFoundError("Photo not found")
        return PhotoResponse(
            id=str(photo.photo_id),
            filename=photo.filename,
            status=photo.status.value,
            analysis=_analysis_response(photo.analysis),
        )

    async def list_photos(
        self, session: AsyncSession, limit: int, offset: int
    ) -> list[PhotoResponse]:
        photos = await self._repository.list(session, limit, offset)
        return [
            PhotoResponse(
                id=str(p.photo_id),
                filename=p.filename,
                status=p.status.value,
                analysis=_analysis_response(p.analysis),
            )
            for p in photos
        ]

    async def get_photo_content(
        self, session: AsyncSession, photo_id: uuid.UUID
    ) -> tuple[bytes, str, str]:
        """Returns (data, content_type, filename). `filename` is included so
        the API layer can set `Content-Disposition: inline; filename=...`
        (tasks/TASK-001/20_design.md §2.4) without importing models/SQL.
        """
        photo = await self._repository.get_by_id(session, photo_id)
        if photo is None:
            raise NotFoundError("Photo not found")

        try:
            async with asyncio.timeout(STORAGE_TIMEOUT_SECONDS):
                data = await anyio.to_thread.run_sync(self._storage.get_file, photo.object_key)
        except TimeoutError as exc:
            raise StorageUnavailable(
                "Storage is unreachable", photo_id=str(photo_id)
            ) from exc

        content_type = self._ext_to_mime(photo.object_key)
        logger.info("content served", extra={"photo_id": str(photo_id)})
        return data, content_type, photo.filename
