"""Contract-lock tests for the TASK-000 skeleton layers.

`PhotoService` and `PhotoRepository` are intentionally unimplemented in
this bootstrap task (real logic lands in TASK-001). These tests exist so
that a future accidental "quick stub" implementation (e.g. a method that
silently returns `None` instead of raising) does not slip through
unnoticed - if any of these methods stop raising `NotImplementedError`,
that is a deliberate TASK-001 change, not a silent regression.
"""

import uuid

import pytest

from app.repositories.photo_repository import PhotoRepository
from app.services.photo_service import PhotoService


class TestPhotoServiceStub:
    async def test_upload_photo_raises_not_implemented(self):
        service = PhotoService(repository=object(), storage=object())

        with pytest.raises(NotImplementedError):
            await service.upload_photo()

    async def test_upload_photo_raises_not_implemented_with_args(self):
        """Signature accepts *args/**kwargs - confirm it still raises
        regardless of what is passed, rather than e.g. crashing on
        argument binding."""
        service = PhotoService(repository=object(), storage=object())

        with pytest.raises(NotImplementedError):
            await service.upload_photo("user1", b"data", content_type="image/jpeg")

    def test_photo_service_stores_repository_and_storage(self):
        repo = object()
        storage = object()
        service = PhotoService(repository=repo, storage=storage)

        assert service._repository is repo
        assert service._storage is storage


class TestPhotoRepositoryStub:
    async def test_create_raises_not_implemented(self):
        repository = PhotoRepository()

        with pytest.raises(NotImplementedError):
            await repository.create(session=object(), photo=object())

    async def test_get_raises_not_implemented(self):
        repository = PhotoRepository()

        with pytest.raises(NotImplementedError):
            await repository.get(session=object(), photo_id=uuid.uuid4())
