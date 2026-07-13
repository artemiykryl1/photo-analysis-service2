"""Business logic layer for the photos domain.

`PhotoService` orchestrates `repositories/` (DB access) and
`integrations/` (MinIO, and later Kafka). It must not know about FastAPI
(`Request`/`Depends`) - callers (api/) prepare all arguments, including an
already-open `AsyncSession`, and pass raw `bytes` rather than an
`UploadFile`.
"""

import asyncio
import logging
import uuid

import anyio.to_thread
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    NotFoundError,
    PayloadTooLargeError,
    StorageUnavailable,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.db.models import Photo, PhotoStatus
from app.integrations.storage import ObjectStorage
from app.repositories.photo_repository import PhotoRepository
from app.schemas.photos import PhotoResponse, UploadPhotoResponse

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB, constitution.md §2.3/§3.3
STORAGE_TIMEOUT_SECONDS = 60  # constitution.md §3.2

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_EXT_TO_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
}


class PhotoService:
    def __init__(self, repository: PhotoRepository, storage: ObjectStorage) -> None:
        self._repository = repository
        self._storage = storage

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
        """
        ext, content_type = self._validate(data)

        photo_id = uuid.uuid4()
        object_key = f"photos/{photo_id}/original.{ext}"

        photo = Photo(
            photo_id=photo_id,
            filename=filename or f"{photo_id}.{ext}",
            object_key=object_key,
            status=PhotoStatus.pending,
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
            logger.warning(
                "MinIO unavailable on upload, rolled back",
                extra={"photo_id": str(photo_id)},
            )
            raise StorageUnavailable("Storage is unreachable", photo_id=str(photo_id)) from exc

        logger.info(
            "stored in MinIO", extra={"photo_id": str(photo_id), "size": len(data)}
        )

        await session.commit()
        logger.info("photo row committed", extra={"photo_id": str(photo_id)})

        return UploadPhotoResponse(photo_id=str(photo_id), status="pending")

    async def get_photo(self, session: AsyncSession, photo_id: uuid.UUID) -> PhotoResponse:
        photo = await self._repository.get_by_id(session, photo_id)
        if photo is None:
            raise NotFoundError("Photo not found")
        return PhotoResponse(id=str(photo.photo_id), filename=photo.filename, status=photo.status.value)

    async def list_photos(
        self, session: AsyncSession, limit: int, offset: int
    ) -> list[PhotoResponse]:
        photos = await self._repository.list(session, limit, offset)
        return [
            PhotoResponse(id=str(p.photo_id), filename=p.filename, status=p.status.value)
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
