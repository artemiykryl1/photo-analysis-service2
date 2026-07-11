"""Business logic layer for the photos domain.

`PhotoService` orchestrates `repositories/` (DB access) and
`integrations/` (MinIO, and later Kafka). It must not know about FastAPI
(`Request`/`Depends`) - callers (api/) prepare all arguments, including an
already-open `AsyncSession`.

TASK-000 scope: skeleton only, no working logic. Real implementation
(validate file, save to MinIO, insert into DB, publish to Kafka) lands in
TASK-001.
"""

from typing import Any

from app.integrations.storage import ObjectStorage
from app.repositories.photo_repository import PhotoRepository


class PhotoService:
    def __init__(self, repository: PhotoRepository, storage: ObjectStorage) -> None:
        self._repository = repository
        self._storage = storage

    async def upload_photo(self, *args: Any, **kwargs: Any) -> Any:
        """Validate, store in MinIO, persist metadata, publish to Kafka.

        TODO(TASK-001): implement. See feature-upload/spec.md §2 for the
        full acceptance criteria (validation, MinIO path, DB transaction,
        Kafka publish, HTTP response shape).
        """
        raise NotImplementedError("upload_photo will be implemented in TASK-001")
