"""Behavioural coverage of `app.services.batch_service`.

`select_best_photo` is the ЗАФИКСИРОВАНО pure function (design §12 step 22,
Приложение) - sort key `is_blurred ASC, blur_score ASC, faces_count DESC,
created_at ASC, photo_id ASC` over `done` photos only. It is exercised
directly against in-memory ORM instances (no DB/session needed - see the
module docstring in `app/services/batch_service.py`).

`BatchService.get_batch` is exercised with a mocked `BatchRepository` (same
style as `PhotoService` tests): 404 on missing batch, `processing` while any
photo is non-terminal, `completed` + write-through `mark_completed` on first
observed completion, and idempotency (no second write) once already
`completed`.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import NotFoundError
from app.db.models import AnalysisResult, Photo, PhotoStatus
from app.services.batch_service import BatchService, select_best_photo

BASE_TIME = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


def _photo(
    *,
    status: PhotoStatus,
    is_blurred: bool | None = None,
    blur_score: float | None = None,
    faces_count: int | None = None,
    created_at: datetime = BASE_TIME,
    photo_id: uuid.UUID | None = None,
) -> Photo:
    photo_id = photo_id or uuid.uuid4()
    photo = Photo(
        photo_id=photo_id,
        filename="a.jpg",
        object_key=f"photos/{photo_id}/original.jpg",
        status=status,
        created_at=created_at,
    )
    if status == PhotoStatus.done:
        photo.analysis = AnalysisResult(
            photo_id=photo_id,
            faces_count=faces_count if faces_count is not None else 1,
            is_blurred=bool(is_blurred),
            blur_score=blur_score if blur_score is not None else 0.5,
            perceptual_hash="abc123",
        )
    else:
        photo.analysis = None
    return photo


class TestSelectBestPhotoEmptyAndAllFailed:
    def test_empty_input_returns_none(self):
        assert select_best_photo([]) is None

    def test_all_failed_returns_none(self):
        photos = [
            _photo(status=PhotoStatus.failed),
            _photo(status=PhotoStatus.failed),
        ]
        assert select_best_photo(photos) is None

    def test_all_pending_or_processing_returns_none(self):
        photos = [
            _photo(status=PhotoStatus.pending),
            _photo(status=PhotoStatus.processing),
        ]
        assert select_best_photo(photos) is None


class TestSelectBestPhotoSingleDone:
    def test_single_done_photo_is_returned(self):
        photo = _photo(status=PhotoStatus.done, is_blurred=False, blur_score=0.2, faces_count=3)
        assert select_best_photo([photo]) == photo.photo_id

    def test_only_done_photos_considered_in_mixed_batch(self):
        done = _photo(status=PhotoStatus.done, is_blurred=False, blur_score=0.1, faces_count=2)
        failed = _photo(status=PhotoStatus.failed)
        pending = _photo(status=PhotoStatus.pending)
        processing = _photo(status=PhotoStatus.processing)

        result = select_best_photo([failed, pending, processing, done])

        assert result == done.photo_id


class TestSelectBestPhotoTieBreakOrder:
    """Sort key (is_blurred ASC, blur_score ASC, faces_count DESC,
    created_at ASC, photo_id ASC) - ЗАФИКСИРОВАНО, tested one axis at a
    time so a regression pinpoints exactly which comparator broke."""

    def test_is_blurred_false_beats_true_regardless_of_other_fields(self):
        sharp = _photo(status=PhotoStatus.done, is_blurred=False, blur_score=0.9, faces_count=0)
        blurry = _photo(status=PhotoStatus.done, is_blurred=True, blur_score=0.01, faces_count=5)

        assert select_best_photo([blurry, sharp]) == sharp.photo_id

    def test_lower_blur_score_wins_when_is_blurred_tied(self):
        low = _photo(status=PhotoStatus.done, is_blurred=False, blur_score=0.1, faces_count=1)
        high = _photo(status=PhotoStatus.done, is_blurred=False, blur_score=0.5, faces_count=1)

        assert select_best_photo([high, low]) == low.photo_id

    def test_more_faces_wins_when_is_blurred_and_blur_score_tied(self):
        few = _photo(status=PhotoStatus.done, is_blurred=False, blur_score=0.3, faces_count=1)
        many = _photo(status=PhotoStatus.done, is_blurred=False, blur_score=0.3, faces_count=5)

        assert select_best_photo([few, many]) == many.photo_id

    def test_earlier_created_at_wins_when_everything_else_tied(self):
        earlier = _photo(
            status=PhotoStatus.done,
            is_blurred=False,
            blur_score=0.3,
            faces_count=2,
            created_at=BASE_TIME,
        )
        later = _photo(
            status=PhotoStatus.done,
            is_blurred=False,
            blur_score=0.3,
            faces_count=2,
            created_at=BASE_TIME + timedelta(seconds=5),
        )

        assert select_best_photo([later, earlier]) == earlier.photo_id

    def test_photo_id_is_final_deterministic_tiebreak_on_complete_tie(self):
        """Review-1 M1: `created_at` is a no-op tie-breaker within a batch
        (all photos share one transaction's `now()`) - `photo_id` must
        still yield a single deterministic winner."""
        id_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_b = uuid.UUID("00000000-0000-0000-0000-000000000002")
        a = _photo(
            status=PhotoStatus.done,
            is_blurred=False,
            blur_score=0.3,
            faces_count=2,
            created_at=BASE_TIME,
            photo_id=id_a,
        )
        b = _photo(
            status=PhotoStatus.done,
            is_blurred=False,
            blur_score=0.3,
            faces_count=2,
            created_at=BASE_TIME,
            photo_id=id_b,
        )

        assert select_best_photo([b, a]) == id_a
        assert select_best_photo([a, b]) == id_a  # order-independent


def _batch(photos: list[Photo], *, status: str = "processing", batch_id: uuid.UUID | None = None):
    batch = MagicMock()
    batch.batch_id = batch_id or uuid.uuid4()
    batch.status = status
    batch.photos = photos
    return batch


class TestGetBatch:
    async def test_raises_not_found_when_batch_missing(self):
        repository = AsyncMock()
        repository.get_by_id.return_value = None
        service = BatchService(batch_repository=repository)

        with pytest.raises(NotFoundError):
            await service.get_batch(session=AsyncMock(), batch_id=uuid.uuid4())

    async def test_processing_status_while_any_photo_non_terminal(self):
        photos = [_photo(status=PhotoStatus.pending), _photo(status=PhotoStatus.done)]
        batch = _batch(photos)
        repository = AsyncMock()
        repository.get_by_id.return_value = batch
        service = BatchService(batch_repository=repository)

        result = await service.get_batch(session=AsyncMock(), batch_id=batch.batch_id)

        assert result.status == "processing"
        assert result.best_photo_id is None
        repository.mark_completed.assert_not_called()

    async def test_completed_status_with_best_photo_id_when_all_terminal(self):
        best = _photo(status=PhotoStatus.done, is_blurred=False, blur_score=0.1, faces_count=3)
        worse = _photo(status=PhotoStatus.done, is_blurred=True, blur_score=0.9, faces_count=1)
        photos = [worse, best]
        batch = _batch(photos)
        repository = AsyncMock()
        repository.get_by_id.return_value = batch
        session = AsyncMock()
        service = BatchService(batch_repository=repository)

        result = await service.get_batch(session=session, batch_id=batch.batch_id)

        assert result.status == "completed"
        assert result.best_photo_id == str(best.photo_id)
        repository.mark_completed.assert_awaited_once_with(session, batch.batch_id, best.photo_id)
        session.commit.assert_awaited_once()

    async def test_completed_with_all_failed_yields_null_best_photo_id(self):
        photos = [_photo(status=PhotoStatus.failed), _photo(status=PhotoStatus.failed)]
        batch = _batch(photos)
        repository = AsyncMock()
        repository.get_by_id.return_value = batch
        service = BatchService(batch_repository=repository)

        result = await service.get_batch(session=AsyncMock(), batch_id=batch.batch_id)

        assert result.status == "completed"
        assert result.best_photo_id is None
        repository.mark_completed.assert_awaited_once()

    async def test_already_completed_batch_does_not_write_through_again(self):
        """Idempotency: a second GET after completion must not call
        `mark_completed`/commit again (design §11 risk #11)."""
        best = _photo(status=PhotoStatus.done, is_blurred=False, blur_score=0.1, faces_count=3)
        batch = _batch([best], status="completed")
        repository = AsyncMock()
        repository.get_by_id.return_value = batch
        session = AsyncMock()
        service = BatchService(batch_repository=repository)

        result = await service.get_batch(session=session, batch_id=batch.batch_id)

        assert result.status == "completed"
        assert result.best_photo_id == str(best.photo_id)
        repository.mark_completed.assert_not_called()
        session.commit.assert_not_called()

    async def test_empty_photos_list_is_treated_as_processing_not_completed(self):
        """`all()` on an empty photos list is vacuously True - the
        implementation explicitly guards against that with `bool(photos)`."""
        batch = _batch([])
        repository = AsyncMock()
        repository.get_by_id.return_value = batch
        service = BatchService(batch_repository=repository)

        result = await service.get_batch(session=AsyncMock(), batch_id=batch.batch_id)

        assert result.status == "processing"
        assert result.photos == []

    async def test_response_includes_analysis_for_done_photos(self):
        done = _photo(status=PhotoStatus.done, is_blurred=False, blur_score=0.42, faces_count=2)
        batch = _batch([done])
        repository = AsyncMock()
        repository.get_by_id.return_value = batch
        service = BatchService(batch_repository=repository)

        result = await service.get_batch(session=AsyncMock(), batch_id=batch.batch_id)

        detail = result.photos[0]
        assert detail.analysis is not None
        assert detail.analysis.blur_score == 0.42
        assert detail.analysis.faces_count == 2
