"""MinIO object storage wrapper.

Photos are stored in MinIO only - the database (`db/models.py::Photo`)
holds nothing but metadata (`s3_path`). This module is the single place
allowed to talk to the MinIO SDK; `services/`/`api/` must go through it.

The underlying `minio` SDK is synchronous. `ensure_bucket` is fine to call
as-is because it only runs once during application startup (lifespan),
outside of the request/response cycle. `save_file`/`get_file` are also
synchronous for now (bootstrap scope) -
TODO(TASK-001): wrap these calls with `anyio.to_thread.run_sync(...)` when
they get invoked from async request handlers, so they do not block the
event loop.
"""

import io
import logging

from minio import Minio
from minio.error import S3Error

from app.core.config import Settings
from app.core.errors import NotFoundError, StorageUnavailable

logger = logging.getLogger(__name__)


class ObjectStorage:
    """Thin wrapper around the MinIO SDK, configured from Settings."""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.MINIO_BUCKET
        self._client = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )

    def ensure_bucket(self) -> None:
        """Idempotently create the configured bucket if it does not exist yet.

        Called once during application startup (lifespan).
        """
        try:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
        except S3Error as exc:
            logger.error("failed to ensure MinIO bucket exists: %s", exc)
            raise StorageUnavailable("MinIO is unreachable") from exc

    def save_file(
        self, object_name: str, data: bytes, length: int, content_type: str
    ) -> str:
        """Store an object in the configured bucket.

        Returns the resulting `s3_path` (`bucket/object_name`).

        TODO(TASK-001): full path/filename validation (no `../`, `//`) and
        multipart upload for large files belongs here.
        """
        try:
            self._client.put_object(
                self._bucket,
                object_name,
                data=io.BytesIO(data),
                length=length,
                content_type=content_type,
            )
        except S3Error as exc:
            logger.error("failed to save object to MinIO: %s", exc)
            raise StorageUnavailable("MinIO is unreachable") from exc
        return f"{self._bucket}/{object_name}"

    def get_file(self, object_name: str) -> bytes:
        """Read an object's bytes from the configured bucket."""
        try:
            response = self._client.get_object(self._bucket, object_name)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                raise NotFoundError("Object not found") from exc
            logger.error("failed to read object from MinIO: %s", exc)
            raise StorageUnavailable("MinIO is unreachable") from exc
