"""MinIO object storage wrapper.

Photos are stored in MinIO only - the database (`db/models.py::Photo`)
holds nothing but metadata (`object_key`). This module is the single place
allowed to talk to the MinIO SDK; `services/`/`api/` must go through it.

The underlying `minio` SDK is synchronous. `ensure_bucket` is fine to call
as-is because it only runs once during application startup (lifespan),
outside of the request/response cycle. `save_file`/`get_file`/`delete_file`
remain synchronous here - callers running inside async request handlers
(services/photo_service.py) are responsible for wrapping calls with
`anyio.to_thread.run_sync(...)` so they do not block the event loop
(see tasks/TASK-001/20_design.md §4.4).
"""

import io
import logging

import urllib3.exceptions
from minio import Minio
from minio.error import S3Error

from app.core.config import Settings
from app.core.errors import NotFoundError, StorageUnavailable

logger = logging.getLogger(__name__)

# `S3Error` is only raised once MinIO has already accepted a TCP connection
# and answered with an S3-protocol-level error (e.g. `NoSuchKey`). When
# MinIO itself is unreachable (pod restarting, connection refused, DNS
# failure, read timeout), the underlying `urllib3`-based transport used by
# the `minio` SDK never gets that far and instead raises one of these
# (TASK-003 review-1 BLOCKING-1: proven by forcing a connection to a closed
# port, which raised `urllib3.exceptions.MaxRetryError` and was previously
# left unhandled, falling through as an unclassified `NO_RETRY`/`UNKNOWN`
# error instead of the retryable `StorageUnavailable` design A2 requires).
_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    urllib3.exceptions.HTTPError,
    ConnectionError,
)
# Deliberately NOT bare `OSError`: builtin `TimeoutError` is also an
# `OSError` subclass, and a gRPC-side/asyncio timeout must keep surfacing as
# `TIMEOUT` (see `analysis_processor.py`/`analyzer_client.py`), not get
# reclassified as a storage failure just because it happens to share a base
# class. `ConnectionError` (`ConnectionRefusedError`/`ConnectionResetError`/
# ...) is unrelated to `TimeoutError` in the builtin hierarchy, so it is
# safe and specific enough to include here.


def _validate_object_name(object_name: str) -> None:
    """Reject object names that could escape the intended prefix.

    `object_key` is always built by the service layer from a fresh uuid4
    (`photos/{photo_id}/original.<ext>`), so in practice this should be
    unreachable - it is defense-in-depth (constitution.md §3.3), not the
    primary sanitization mechanism. Raises `ValueError` (a programming
    error, not a runtime/user-facing condition) rather than a domain
    `AppError`, since untrusted input never reaches this function.
    """
    if ".." in object_name or "//" in object_name:
        raise ValueError(f"unsafe object name: {object_name!r}")


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
        except _TRANSPORT_ERRORS as exc:
            logger.error("MinIO unreachable while ensuring bucket exists: %s", exc)
            raise StorageUnavailable("MinIO is unreachable") from exc

    def save_file(
        self, object_name: str, data: bytes, length: int, content_type: str
    ) -> str:
        """Store an object in the configured bucket.

        Returns the resulting `bucket/object_key` (informational only -
        callers persist the `object_name` they passed in, not this
        return value).
        """
        _validate_object_name(object_name)
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
        except _TRANSPORT_ERRORS as exc:
            logger.error("MinIO unreachable while saving object: %s", exc)
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
        except _TRANSPORT_ERRORS as exc:
            # MinIO did not answer at all (connection refused / DNS failure /
            # read timeout) - the SDK raises `urllib3.exceptions.MaxRetryError`
            # or similar here, never an `S3Error`. This is exactly the
            # "MinIO is unreachable" case TASK-003 A2 requires to be
            # retryable, not a permanent `UNKNOWN` failure.
            logger.error("MinIO unreachable while reading object: %s", exc)
            raise StorageUnavailable("MinIO is unreachable") from exc

    def delete_file(self, object_name: str) -> None:
        """Best-effort delete, used as compensation for an orphaned object
        (e.g. MinIO save succeeded but the DB commit that should follow it
        failed). Swallows `NoSuchKey` - deleting an already-absent object
        is not an error for a compensating action.
        """
        try:
            self._client.remove_object(self._bucket, object_name)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return
            logger.warning("failed to delete orphaned object from MinIO: %s", exc)
