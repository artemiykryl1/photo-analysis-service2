"""Behavioural coverage of `PhotoService.create_batch` (TASK-002 block B,
design §12 step 21, §13.2 "Батч-валидация").

Same fake-repository/fake-storage style as tests/test_photo_service.py -
no real Postgres/MinIO connection. Focus: 2-10 file-count validation,
per-file magic-byte validation, the `BATCH_MAX_TOTAL_BYTES` guard (review-2
fix #6), and atomicity - any rejection must leave zero DB rows and zero
MinIO objects created.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.photo_service as photo_service_module
from app.core.errors import (
    BatchSizeError,
    DatabaseUnavailable,
    PayloadTooLargeError,
    StorageUnavailable,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.services.photo_service import PhotoService

JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 20
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
NOT_AN_IMAGE = b"plain text, not an image"


def _service(repository=None, storage=None, batch_repository=None) -> PhotoService:
    return PhotoService(
        repository=repository or AsyncMock(),
        storage=storage or MagicMock(),
        batch_repository=batch_repository or AsyncMock(),
    )


class TestBatchSizeValidation:
    async def test_zero_files_raises_batch_size_error(self):
        service = _service()
        with pytest.raises(BatchSizeError):
            await service.create_batch(AsyncMock(), [])

    async def test_one_file_raises_batch_size_error(self):
        service = _service()
        with pytest.raises(BatchSizeError):
            await service.create_batch(AsyncMock(), [("a.jpg", JPEG_BYTES)])

    async def test_eleven_files_raises_batch_size_error(self):
        service = _service()
        files = [(f"{i}.jpg", JPEG_BYTES) for i in range(11)]
        with pytest.raises(BatchSizeError):
            await service.create_batch(AsyncMock(), files)

    async def test_two_files_is_the_minimum_accepted(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)

        result = await service.create_batch(
            AsyncMock(), [("a.jpg", JPEG_BYTES), ("b.jpg", JPEG_BYTES)]
        )

        assert len(result.photos) == 2

    async def test_ten_files_is_the_maximum_accepted(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)
        files = [(f"{i}.jpg", JPEG_BYTES) for i in range(10)]

        result = await service.create_batch(AsyncMock(), files)

        assert len(result.photos) == 10

    async def test_out_of_range_size_touches_neither_repository_nor_storage(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)

        with pytest.raises(BatchSizeError):
            await service.create_batch(AsyncMock(), [("a.jpg", JPEG_BYTES)])

        repository.create.assert_not_called()
        batch_repository.create.assert_not_called()
        storage.save_file.assert_not_called()


class TestBatchPerFileValidationAtomicity:
    async def test_invalid_file_anywhere_rejects_whole_batch_before_any_write(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)
        files = [("good.jpg", JPEG_BYTES), ("bad.txt", NOT_AN_IMAGE)]

        with pytest.raises(UnsupportedMediaTypeError):
            await service.create_batch(AsyncMock(), files)

        repository.create.assert_not_called()
        batch_repository.create.assert_not_called()
        storage.save_file.assert_not_called()

    async def test_empty_file_in_batch_rejects_whole_batch(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)
        files = [("good.jpg", JPEG_BYTES), ("empty.jpg", b"")]

        with pytest.raises(ValidationError):
            await service.create_batch(AsyncMock(), files)

        repository.create.assert_not_called()
        batch_repository.create.assert_not_called()
        storage.save_file.assert_not_called()

    async def test_oversized_single_file_in_batch_rejects_whole_batch(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)
        oversized = JPEG_BYTES + b"\x00" * (51 * 1024 * 1024)
        files = [("good.jpg", JPEG_BYTES), ("big.jpg", oversized)]

        with pytest.raises(PayloadTooLargeError):
            await service.create_batch(AsyncMock(), files)

        repository.create.assert_not_called()
        batch_repository.create.assert_not_called()
        storage.save_file.assert_not_called()

    async def test_invalid_file_is_detected_even_when_first_in_the_list(self):
        """Validation runs for ALL files before any write, regardless of
        position - not just "stop at the first bad one and keep whatever
        came before"."""
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)
        files = [("bad.txt", NOT_AN_IMAGE), ("good.jpg", JPEG_BYTES)]

        with pytest.raises(UnsupportedMediaTypeError):
            await service.create_batch(AsyncMock(), files)

        storage.save_file.assert_not_called()


class TestBatchTotalSizeLimit:
    async def test_total_size_over_limit_raises_payload_too_large_before_any_write(
        self, monkeypatch
    ):
        monkeypatch.setattr(photo_service_module, "BATCH_MAX_TOTAL_BYTES", 100)
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)
        files = [("a.jpg", JPEG_BYTES + b"\x00" * 60), ("b.jpg", JPEG_BYTES + b"\x00" * 60)]

        with pytest.raises(PayloadTooLargeError):
            await service.create_batch(AsyncMock(), files)

        repository.create.assert_not_called()
        batch_repository.create.assert_not_called()
        storage.save_file.assert_not_called()

    async def test_total_size_at_or_under_limit_is_accepted(self, monkeypatch):
        monkeypatch.setattr(photo_service_module, "BATCH_MAX_TOTAL_BYTES", 1000)
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)
        files = [("a.jpg", JPEG_BYTES), ("b.jpg", JPEG_BYTES)]  # well under 1000 bytes total

        result = await service.create_batch(AsyncMock(), files)

        assert len(result.photos) == 2


class TestBatchHappyPath:
    async def test_creates_one_photo_row_per_file_with_shared_batch_id(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)

        await service.create_batch(
            AsyncMock(), [("a.jpg", JPEG_BYTES), ("b.png", PNG_BYTES)]
        )

        assert repository.create.await_count == 2
        photo_args = [call.args[1] for call in repository.create.call_args_list]
        batch_ids = {p.batch_id for p in photo_args}
        assert len(batch_ids) == 1  # every photo in the batch shares one batch_id

    async def test_response_lists_each_photo_id_with_pending_status(self):
        service = _service()

        result = await service.create_batch(
            AsyncMock(), [("a.jpg", JPEG_BYTES), ("b.jpg", JPEG_BYTES)]
        )

        assert len(result.photos) == 2
        for item in result.photos:
            uuid.UUID(item.photo_id)
            assert item.status == "pending"

    async def test_each_file_saved_to_storage_with_its_own_object_key(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)

        await service.create_batch(
            AsyncMock(), [("a.jpg", JPEG_BYTES), ("b.png", PNG_BYTES)]
        )

        assert storage.save_file.call_count == 2
        object_keys = {call.args[0] for call in storage.save_file.call_args_list}
        assert len(object_keys) == 2

    async def test_commits_exactly_once(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)
        session = AsyncMock()

        await service.create_batch(session, [("a.jpg", JPEG_BYTES), ("b.jpg", JPEG_BYTES)])

        session.commit.assert_awaited_once()


class TestBatchStorageFailureRollback:
    async def test_storage_failure_rolls_back_and_deletes_already_saved_objects(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        storage.save_file.side_effect = [None, StorageUnavailable("minio down")]
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)
        session = AsyncMock()

        with pytest.raises(StorageUnavailable):
            await service.create_batch(
                session, [("a.jpg", JPEG_BYTES), ("b.jpg", JPEG_BYTES), ("c.jpg", JPEG_BYTES)]
            )

        session.rollback.assert_awaited_once()
        session.commit.assert_not_called()
        storage.delete_file.assert_called_once()  # only the first (already-saved) object

    async def test_storage_failure_on_first_file_deletes_nothing(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        storage.save_file.side_effect = StorageUnavailable("minio down")
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)
        session = AsyncMock()

        with pytest.raises(StorageUnavailable):
            await service.create_batch(session, [("a.jpg", JPEG_BYTES), ("b.jpg", JPEG_BYTES)])

        storage.delete_file.assert_not_called()
        session.rollback.assert_awaited_once()

    async def test_delete_failure_on_first_saved_object_does_not_skip_the_rest(self):
        """review-1 m3: the compensation loop in the StorageUnavailable path
        must be best-effort per object - one failing `delete_file` must not
        stop the remaining already-saved objects from being deleted, and the
        original `StorageUnavailable` must still be what escapes (not the
        delete's own exception)."""
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        # First 3 files save fine, the 4th blows up with StorageUnavailable.
        storage.save_file.side_effect = [None, None, None, StorageUnavailable("minio down")]
        storage.delete_file.side_effect = [RuntimeError("minio also unreachable"), None, None]
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)
        session = AsyncMock()

        with pytest.raises(StorageUnavailable):
            await service.create_batch(
                session,
                [
                    ("a.jpg", JPEG_BYTES),
                    ("b.jpg", JPEG_BYTES),
                    ("c.jpg", JPEG_BYTES),
                    ("d.jpg", JPEG_BYTES),
                ],
            )

        # All 3 already-saved objects were attempted, even though the first
        # delete failed.
        assert storage.delete_file.call_count == 3


class TestBatchCommitFailureCompensation:
    """TASK-002.1 F5: by the time the final `session.commit()` runs, every
    file in the batch is already saved to MinIO - a failed commit must
    compensate by deleting ALL of them (not just one), roll back, and
    surface `DatabaseUnavailable` (503)."""

    async def test_commit_failure_deletes_every_saved_object_and_raises_database_unavailable(
        self,
    ):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        session = AsyncMock()
        session.commit.side_effect = ConnectionError("db connection lost")
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)

        with pytest.raises(DatabaseUnavailable):
            await service.create_batch(
                session, [("a.jpg", JPEG_BYTES), ("b.jpg", JPEG_BYTES), ("c.jpg", JPEG_BYTES)]
            )

        session.rollback.assert_awaited_once()
        assert storage.delete_file.call_count == 3  # every saved object, not just the first
        saved_keys = {call.args[0] for call in storage.save_file.call_args_list}
        deleted_keys = {call.args[0] for call in storage.delete_file.call_args_list}
        assert deleted_keys == saved_keys

    async def test_delete_failure_during_compensation_does_not_mask_database_unavailable(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        storage.delete_file.side_effect = RuntimeError("minio also unreachable")
        session = AsyncMock()
        session.commit.side_effect = ConnectionError("db connection lost")
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)

        with pytest.raises(DatabaseUnavailable):
            await service.create_batch(session, [("a.jpg", JPEG_BYTES), ("b.jpg", JPEG_BYTES)])

        # best-effort: it still tries every key even after one delete fails
        assert storage.delete_file.call_count == 2

    async def test_successful_commit_never_triggers_compensation(self):
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)
        session = AsyncMock()

        await service.create_batch(session, [("a.jpg", JPEG_BYTES), ("b.jpg", JPEG_BYTES)])

        storage.delete_file.assert_not_called()

    async def test_rollback_itself_failing_still_deletes_every_object_and_raises_503(self):
        """review-1 m2: same guarantee as the single-upload path - a
        `rollback()` failure must not skip the best-effort cleanup of every
        already-saved object, nor prevent `DatabaseUnavailable` (503) from
        being raised."""
        repository = AsyncMock()
        batch_repository = AsyncMock()
        storage = MagicMock()
        session = AsyncMock()
        session.commit.side_effect = ConnectionError("db connection lost")
        session.rollback.side_effect = RuntimeError("connection already invalidated")
        service = _service(repository=repository, storage=storage, batch_repository=batch_repository)

        with pytest.raises(DatabaseUnavailable):
            await service.create_batch(
                session, [("a.jpg", JPEG_BYTES), ("b.jpg", JPEG_BYTES), ("c.jpg", JPEG_BYTES)]
            )

        assert storage.delete_file.call_count == 3
