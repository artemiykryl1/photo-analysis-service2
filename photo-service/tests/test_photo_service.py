"""Full behavioural coverage of `app.services.photo_service.PhotoService`.

`test_stubs.py` keeps only a couple of legacy smoke checks (construction,
`_validate`, 404 on missing photo). This module is the real functional
suite for the service layer: happy paths, validation edge cases, the
DB(flush)->MinIO(save)->commit ordering and rollback-on-storage-failure
contract from tasks/TASK-001/20_design.md §8, and photo_id-tagged logging
(constitution.md §3.1 / feature-upload DoD).

The repository and storage collaborators are always fakes/mocks here -
no real Postgres/MinIO connection is made (that would belong to the
dedicated migration integration test / a live docker-compose run).
"""

import logging
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.photo_service as photo_service_module
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    StorageUnavailable,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.db.models import PhotoStatus
from app.services.photo_service import PhotoService

JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 20
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


def _service(repository=None, storage=None) -> PhotoService:
    return PhotoService(repository=repository or AsyncMock(), storage=storage or MagicMock())


class TestValidate:
    """Additional edge cases beyond the smoke tests kept in test_stubs.py."""

    def test_validate_rejects_file_over_50mb(self):
        service = _service()
        oversized = JPEG_BYTES + b"\x00" * (50 * 1024 * 1024)

        with pytest.raises(PayloadTooLargeError):
            service._validate(oversized)

    def test_validate_accepts_file_exactly_at_50mb_boundary(self):
        service = _service()
        # exactly MAX_FILE_SIZE_BYTES, valid JPEG signature -> must be accepted (only "> max" rejects)
        data = _JPEG_MAGIC_PADDED(photo_service_module.MAX_FILE_SIZE_BYTES)

        ext, mime = service._validate(data)

        assert (ext, mime) == ("jpg", "image/jpeg")

    def test_validate_order_is_empty_before_size_before_signature(self):
        """Order per design §5: empty -> 400 takes priority even if huge."""
        service = _service()
        with pytest.raises(ValidationError):
            service._validate(b"")

    def test_validate_rejects_text_disguised_with_image_extension_by_content(self):
        """Content, not extension, decides the type - there is no extension
        argument to `_validate` at all, only bytes."""
        service = _service()
        with pytest.raises(UnsupportedMediaTypeError):
            service._validate(b"this is just plain text pretending to be a photo")


def _JPEG_MAGIC_PADDED(total_size: int) -> bytes:
    return b"\xff\xd8\xff" + b"\x00" * (total_size - 3)


class TestExtToMime:
    @pytest.mark.parametrize(
        "object_key, expected_mime",
        [
            ("photos/x/original.jpg", "image/jpeg"),
            ("photos/x/original.jpeg", "image/jpeg"),
            ("photos/x/original.png", "image/png"),
            ("photos/x/original.JPG", "image/jpeg"),
            ("photos/x/original.bin", "application/octet-stream"),
        ],
    )
    def test_ext_to_mime_mapping(self, object_key, expected_mime):
        assert PhotoService._ext_to_mime(object_key) == expected_mime


class TestCreatePhotoHappyPath:
    async def test_jpeg_upload_returns_pending_response(self):
        repository = AsyncMock()
        storage = MagicMock()
        service = PhotoService(repository=repository, storage=storage)
        session = AsyncMock()

        result = await service.create_photo(session, "cat.jpg", JPEG_BYTES)

        assert result.status == "pending"
        uuid.UUID(result.photo_id)  # must be a valid uuid string

    async def test_png_upload_returns_pending_response(self):
        repository = AsyncMock()
        storage = MagicMock()
        service = PhotoService(repository=repository, storage=storage)
        session = AsyncMock()

        result = await service.create_photo(session, "cat.png", PNG_BYTES)

        assert result.status == "pending"
        uuid.UUID(result.photo_id)

    async def test_object_key_matches_photos_id_original_ext_contract(self):
        repository = AsyncMock()
        storage = MagicMock()
        service = PhotoService(repository=repository, storage=storage)
        session = AsyncMock()

        result = await service.create_photo(session, "cat.jpg", JPEG_BYTES)

        photo_arg = repository.create.call_args.args[1]
        assert photo_arg.object_key == f"photos/{result.photo_id}/original.jpg"
        assert photo_arg.status == PhotoStatus.pending

    async def test_storage_save_file_called_with_object_key_and_bytes(self):
        repository = AsyncMock()
        storage = MagicMock()
        service = PhotoService(repository=repository, storage=storage)
        session = AsyncMock()

        result = await service.create_photo(session, "cat.jpg", JPEG_BYTES)

        storage.save_file.assert_called_once()
        call = storage.save_file.call_args
        assert call.args[0] == f"photos/{result.photo_id}/original.jpg"
        assert call.args[1] == JPEG_BYTES
        assert call.args[2] == len(JPEG_BYTES)
        assert call.args[3] == "image/jpeg"

    async def test_missing_filename_falls_back_to_photo_id_dot_ext(self):
        repository = AsyncMock()
        storage = MagicMock()
        service = PhotoService(repository=repository, storage=storage)
        session = AsyncMock()

        result = await service.create_photo(session, None, JPEG_BYTES)

        photo_arg = repository.create.call_args.args[1]
        assert photo_arg.filename == f"{result.photo_id}.jpg"


class TestCreatePhotoValidationErrorsPropagate:
    async def test_empty_file_raises_validation_error_before_touching_repo_or_storage(self):
        repository = AsyncMock()
        storage = MagicMock()
        service = PhotoService(repository=repository, storage=storage)
        session = AsyncMock()

        with pytest.raises(ValidationError):
            await service.create_photo(session, "empty.jpg", b"")

        repository.create.assert_not_called()
        storage.save_file.assert_not_called()
        session.commit.assert_not_called()

    async def test_oversized_file_raises_payload_too_large(self):
        repository = AsyncMock()
        storage = MagicMock()
        service = PhotoService(repository=repository, storage=storage)
        session = AsyncMock()
        oversized = JPEG_BYTES + b"\x00" * (51 * 1024 * 1024)

        with pytest.raises(PayloadTooLargeError):
            await service.create_photo(session, "big.jpg", oversized)

        repository.create.assert_not_called()
        storage.save_file.assert_not_called()

    async def test_non_image_raises_unsupported_media_type(self):
        repository = AsyncMock()
        storage = MagicMock()
        service = PhotoService(repository=repository, storage=storage)
        session = AsyncMock()

        with pytest.raises(UnsupportedMediaTypeError):
            await service.create_photo(session, "notes.txt", b"plain text content")

        repository.create.assert_not_called()
        storage.save_file.assert_not_called()


class TestCreatePhotoOrderAndRollback:
    """tasks/TASK-001/20_design.md §8: DB(add+flush) -> MinIO(save) ->
    commit, with rollback (no commit) on storage failure/timeout."""

    async def test_calls_happen_in_order_flush_then_save_then_commit(self):
        order: list[str] = []

        repository = AsyncMock()

        async def create_side_effect(session, photo):
            order.append("db_flush")
            return photo

        repository.create.side_effect = create_side_effect

        storage = MagicMock()

        def save_side_effect(*_args, **_kwargs):
            order.append("minio_save")

        storage.save_file.side_effect = save_side_effect

        session = AsyncMock()

        async def commit_side_effect():
            order.append("commit")

        session.commit.side_effect = commit_side_effect

        service = PhotoService(repository=repository, storage=storage)

        await service.create_photo(session, "a.jpg", JPEG_BYTES)

        assert order == ["db_flush", "minio_save", "commit"]

    async def test_conflict_error_from_repository_propagates_without_touching_storage(self):
        repository = AsyncMock()
        repository.create.side_effect = ConflictError("Photo already exists")
        storage = MagicMock()
        session = AsyncMock()
        service = PhotoService(repository=repository, storage=storage)

        with pytest.raises(ConflictError):
            await service.create_photo(session, "a.jpg", JPEG_BYTES)

        storage.save_file.assert_not_called()
        session.commit.assert_not_called()

    async def test_storage_unavailable_rolls_back_and_does_not_commit(self):
        repository = AsyncMock()
        storage = MagicMock()
        storage.save_file.side_effect = StorageUnavailable("MinIO is down")
        session = AsyncMock()
        service = PhotoService(repository=repository, storage=storage)

        with pytest.raises(StorageUnavailable):
            await service.create_photo(session, "a.jpg", JPEG_BYTES)

        session.rollback.assert_awaited_once()
        session.commit.assert_not_called()

    async def test_storage_timeout_is_converted_to_storage_unavailable_and_rolls_back(
        self, monkeypatch
    ):
        monkeypatch.setattr(photo_service_module, "STORAGE_TIMEOUT_SECONDS", 0.05)
        repository = AsyncMock()
        storage = MagicMock()

        def slow_save(*_args, **_kwargs):
            time.sleep(0.3)

        storage.save_file.side_effect = slow_save
        session = AsyncMock()
        service = PhotoService(repository=repository, storage=storage)

        with pytest.raises(StorageUnavailable):
            await service.create_photo(session, "a.jpg", JPEG_BYTES)

        session.rollback.assert_awaited_once()
        session.commit.assert_not_called()

    async def test_repository_create_still_called_before_storage_failure(self):
        """Even on a storage failure, the row must have been flushed first
        (that's what makes rollback meaningful - there is something to
        undo)."""
        repository = AsyncMock()
        storage = MagicMock()
        storage.save_file.side_effect = StorageUnavailable("down")
        session = AsyncMock()
        service = PhotoService(repository=repository, storage=storage)

        with pytest.raises(StorageUnavailable):
            await service.create_photo(session, "a.jpg", JPEG_BYTES)

        repository.create.assert_awaited_once()


class TestGetPhoto:
    async def test_returns_photo_response_when_found(self):
        photo_id = uuid.uuid4()
        found = MagicMock()
        found.photo_id = photo_id
        found.filename = "cat.jpg"
        found.status = PhotoStatus.processing
        found.analysis = None  # status=processing -> not analyzed yet

        repository = AsyncMock()
        repository.get_by_id.return_value = found
        service = PhotoService(repository=repository, storage=MagicMock())

        result = await service.get_photo(session=AsyncMock(), photo_id=photo_id)

        assert result.id == str(photo_id)
        assert result.filename == "cat.jpg"
        assert result.status == "processing"
        assert result.analysis is None

    async def test_raises_not_found_when_missing(self):
        repository = AsyncMock()
        repository.get_by_id.return_value = None
        service = PhotoService(repository=repository, storage=MagicMock())

        with pytest.raises(NotFoundError):
            await service.get_photo(session=AsyncMock(), photo_id=uuid.uuid4())


class TestListPhotos:
    async def test_empty_repository_list_returns_empty_list(self):
        repository = AsyncMock()
        repository.list.return_value = []
        service = PhotoService(repository=repository, storage=MagicMock())

        result = await service.list_photos(session=AsyncMock(), limit=50, offset=0)

        assert result == []

    async def test_maps_each_orm_row_to_photo_response(self):
        p1 = MagicMock(
            photo_id=uuid.uuid4(), filename="a.jpg", status=PhotoStatus.pending, analysis=None
        )
        p2 = MagicMock(
            photo_id=uuid.uuid4(), filename="b.png", status=PhotoStatus.done, analysis=None
        )
        repository = AsyncMock()
        repository.list.return_value = [p1, p2]
        service = PhotoService(repository=repository, storage=MagicMock())

        result = await service.list_photos(session=AsyncMock(), limit=50, offset=0)

        assert [r.id for r in result] == [str(p1.photo_id), str(p2.photo_id)]
        assert [r.status for r in result] == ["pending", "done"]

    async def test_forwards_limit_and_offset_to_repository(self):
        repository = AsyncMock()
        repository.list.return_value = []
        service = PhotoService(repository=repository, storage=MagicMock())
        session = AsyncMock()

        await service.list_photos(session, limit=10, offset=20)

        repository.list.assert_awaited_once_with(session, 10, 20)


class TestGetPhotoContent:
    async def test_raises_not_found_when_record_missing_and_never_calls_storage(self):
        repository = AsyncMock()
        repository.get_by_id.return_value = None
        storage = MagicMock()
        service = PhotoService(repository=repository, storage=storage)

        with pytest.raises(NotFoundError):
            await service.get_photo_content(session=AsyncMock(), photo_id=uuid.uuid4())

        storage.get_file.assert_not_called()

    async def test_returns_bytes_content_type_and_filename_when_found(self):
        photo_id = uuid.uuid4()
        found = MagicMock()
        found.object_key = f"photos/{photo_id}/original.png"
        found.filename = "cat.png"

        repository = AsyncMock()
        repository.get_by_id.return_value = found
        storage = MagicMock()
        storage.get_file.return_value = PNG_BYTES
        service = PhotoService(repository=repository, storage=storage)

        data, content_type, filename = await service.get_photo_content(
            session=AsyncMock(), photo_id=photo_id
        )

        assert data == PNG_BYTES
        assert content_type == "image/png"
        assert filename == "cat.png"
        storage.get_file.assert_called_once_with(found.object_key)

    async def test_object_missing_in_storage_propagates_not_found(self):
        """`storage.get_file` already maps MinIO's NoSuchKey to NotFoundError
        (see test_storage.py); the service must let it propagate as-is."""
        found = MagicMock()
        found.object_key = "photos/x/original.jpg"
        found.filename = "x.jpg"

        repository = AsyncMock()
        repository.get_by_id.return_value = found
        storage = MagicMock()
        storage.get_file.side_effect = NotFoundError("Object not found")
        service = PhotoService(repository=repository, storage=storage)

        with pytest.raises(NotFoundError):
            await service.get_photo_content(session=AsyncMock(), photo_id=uuid.uuid4())

    async def test_storage_unavailable_propagates(self):
        found = MagicMock()
        found.object_key = "photos/x/original.jpg"
        found.filename = "x.jpg"

        repository = AsyncMock()
        repository.get_by_id.return_value = found
        storage = MagicMock()
        storage.get_file.side_effect = StorageUnavailable("MinIO is unreachable")
        service = PhotoService(repository=repository, storage=storage)

        with pytest.raises(StorageUnavailable):
            await service.get_photo_content(session=AsyncMock(), photo_id=uuid.uuid4())

    async def test_storage_timeout_is_converted_to_storage_unavailable(self, monkeypatch):
        monkeypatch.setattr(photo_service_module, "STORAGE_TIMEOUT_SECONDS", 0.05)
        found = MagicMock()
        found.object_key = "photos/x/original.jpg"
        found.filename = "x.jpg"

        repository = AsyncMock()
        repository.get_by_id.return_value = found
        storage = MagicMock()

        def slow_get(*_args, **_kwargs):
            time.sleep(0.3)
            return PNG_BYTES

        storage.get_file.side_effect = slow_get
        service = PhotoService(repository=repository, storage=storage)

        with pytest.raises(StorageUnavailable):
            await service.get_photo_content(session=AsyncMock(), photo_id=uuid.uuid4())


class TestObservability:
    """constitution.md §3.1 / feature-upload DoD: key log lines must carry
    `photo_id` so an operator can grep a single upload's lifecycle."""

    async def test_create_photo_logs_carry_photo_id(self, caplog):
        repository = AsyncMock()
        storage = MagicMock()
        service = PhotoService(repository=repository, storage=storage)
        session = AsyncMock()

        with caplog.at_level(logging.INFO, logger="app.services.photo_service"):
            result = await service.create_photo(session, "a.jpg", JPEG_BYTES)

        tagged = [
            r for r in caplog.records if getattr(r, "photo_id", None) == result.photo_id
        ]
        assert len(tagged) >= 3  # "upload received", "stored in MinIO", "photo row committed"

    async def test_rollback_warning_log_carries_photo_id(self, caplog):
        repository = AsyncMock()
        storage = MagicMock()
        storage.save_file.side_effect = StorageUnavailable("down")
        session = AsyncMock()
        service = PhotoService(repository=repository, storage=storage)

        with caplog.at_level(logging.INFO, logger="app.services.photo_service"):
            with pytest.raises(StorageUnavailable):
                await service.create_photo(session, "a.jpg", JPEG_BYTES)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "expected a WARNING log on storage failure"
        assert all(hasattr(r, "photo_id") and r.photo_id for r in warnings)

    async def test_content_served_log_carries_photo_id(self, caplog):
        photo_id = uuid.uuid4()
        found = MagicMock()
        found.object_key = "photos/x/original.jpg"
        found.filename = "x.jpg"
        repository = AsyncMock()
        repository.get_by_id.return_value = found
        storage = MagicMock()
        storage.get_file.return_value = JPEG_BYTES
        service = PhotoService(repository=repository, storage=storage)

        with caplog.at_level(logging.INFO, logger="app.services.photo_service"):
            await service.get_photo_content(session=AsyncMock(), photo_id=photo_id)

        tagged = [
            r for r in caplog.records if getattr(r, "photo_id", None) == str(photo_id)
        ]
        assert tagged
