"""Smoke tests confirming the TASK-000 skeleton layers are now implemented.

`PhotoService`/`PhotoRepository` were intentionally unimplemented in
TASK-000 (stubs raising `NotImplementedError`). TASK-001 implements the
real logic; this file keeps only the still-relevant construction check
and adds a couple of lightweight behavioural smoke tests. Full coverage
(happy paths, error paths, DB round-trips) is added by test-writer per
tasks/TASK-001/20_design.md §11.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.core.errors import NotFoundError, UnsupportedMediaTypeError, ValidationError
from app.repositories.photo_repository import PhotoRepository
from app.services.photo_service import PhotoService


class TestPhotoServiceConstruction:
    def test_photo_service_stores_repository_and_storage(self):
        repo = object()
        storage = object()
        service = PhotoService(repository=repo, storage=storage)

        assert service._repository is repo
        assert service._storage is storage


class TestPhotoServiceValidate:
    def test_validate_rejects_empty_file(self):
        service = PhotoService(repository=object(), storage=object())
        with pytest.raises(ValidationError):
            service._validate(b"")

    def test_validate_rejects_unknown_signature(self):
        service = PhotoService(repository=object(), storage=object())
        with pytest.raises(UnsupportedMediaTypeError):
            service._validate(b"not-an-image")

    def test_validate_accepts_jpeg_magic_bytes(self):
        service = PhotoService(repository=object(), storage=object())
        ext, mime = service._validate(b"\xff\xd8\xff" + b"\x00" * 10)
        assert (ext, mime) == ("jpg", "image/jpeg")

    def test_validate_accepts_png_magic_bytes(self):
        service = PhotoService(repository=object(), storage=object())
        ext, mime = service._validate(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        assert (ext, mime) == ("png", "image/png")


class TestPhotoServiceGetPhotoNotFound:
    async def test_get_photo_raises_not_found_when_missing(self):
        repository = AsyncMock()
        repository.get_by_id.return_value = None
        service = PhotoService(repository=repository, storage=object())

        with pytest.raises(NotFoundError):
            await service.get_photo(session=object(), photo_id=uuid.uuid4())


class TestPhotoRepositoryConstruction:
    def test_repository_has_expected_methods(self):
        repository = PhotoRepository()
        assert hasattr(repository, "create")
        assert hasattr(repository, "get_by_id")
        assert hasattr(repository, "list")
        assert hasattr(repository, "update_status")
