"""Behavioural coverage of `app.services.batch_service`.

`select_best_photo` is the ЗАФИКСИРОВАНО pure function, redefined by
TASK-003 (tasks/TASK-003/20_design.md §3) - sort key `sharp_enough
(blur_score >= SHARPNESS_THRESHOLD) DESC, faces_count DESC,
eyes_closed_count ASC (NULL=0), blur_score DESC, created_at ASC, photo_id
ASC` over `done` photos only. It is exercised directly against in-memory
ORM instances (no DB/session needed - see the module docstring in
`app/services/batch_service.py`).

`BatchService.get_batch` is exercised with a mocked `BatchRepository` (same
style as `PhotoService` tests): 404 on missing batch, `processing` while any
photo is non-terminal, `completed` + computed `best_photo_id` once all
photos are terminal. TASK-002.1 (F3): `get_batch` is strictly read-only -
it never calls `repository.try_complete` nor `session.commit`, regardless
of whether the batch is freshly completed or already was; persisting the
completion is now the worker's job (see `test_analysis_processor.py` /
`test_batch_repository.py::TestTryComplete`).
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import NotFoundError
from app.db.models import AnalysisResult, Photo, PhotoStatus
from app.services.batch_service import SHARPNESS_THRESHOLD, BatchService, select_best_photo

BASE_TIME = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

# Comfortably on either side of SHARPNESS_THRESHOLD (100.0) - matches the
# spike A3 measurements the threshold was picked from (blurry 0.4-2.9,
# sharp 930-99 774).
SHARP_SCORE = 930.0
BLURRY_SCORE = 1.1


def _photo(
    *,
    status: PhotoStatus,
    is_blurred: bool | None = None,
    blur_score: float | None = None,
    faces_count: int | None = None,
    eyes_closed_count: int | None = None,
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
            blur_score=blur_score if blur_score is not None else SHARP_SCORE,
            perceptual_hash="abc123",
            eyes_closed_count=eyes_closed_count,
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
        photo = _photo(
            status=PhotoStatus.done, is_blurred=False, blur_score=SHARP_SCORE, faces_count=3
        )
        assert select_best_photo([photo]) == photo.photo_id

    def test_only_done_photos_considered_in_mixed_batch(self):
        done = _photo(
            status=PhotoStatus.done, is_blurred=False, blur_score=SHARP_SCORE, faces_count=2
        )
        failed = _photo(status=PhotoStatus.failed)
        pending = _photo(status=PhotoStatus.pending)
        processing = _photo(status=PhotoStatus.processing)

        result = select_best_photo([failed, pending, processing, done])

        assert result == done.photo_id


class TestSelectBestPhotoTieBreakOrder:
    """Sort key (TASK-003 redaction, design §3.2) - `sharp_enough
    (blur_score >= SHARPNESS_THRESHOLD) DESC, faces_count DESC,
    eyes_closed_count ASC (NULL=0), blur_score DESC, created_at ASC,
    photo_id ASC` - ЗАФИКСИРОВАНО, tested one axis at a time so a
    regression pinpoints exactly which comparator broke."""

    def test_is_blurred_does_not_affect_the_outcome(self):
        """TASK-003 design §3.4: `is_blurred` is deliberately NOT part of
        the formula (spike A3 found it always `True`, including for the
        sharpest sample) - two otherwise-identical candidates must tie
        regardless of what `is_blurred` says, with `photo_id` deciding."""
        marked_blurred = _photo(
            status=PhotoStatus.done,
            is_blurred=True,
            blur_score=SHARP_SCORE,
            faces_count=2,
            photo_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        )
        marked_sharp = _photo(
            status=PhotoStatus.done,
            is_blurred=False,
            blur_score=SHARP_SCORE,
            faces_count=2,
            photo_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        )

        # Only photo_id (the final tie-break) distinguishes them - is_blurred is ignored.
        assert select_best_photo([marked_sharp, marked_blurred]) == marked_blurred.photo_id

    def test_sharp_bucket_beats_blurry_bucket_even_with_worse_other_fields(self):
        """First key is the coarse sharp/blurry bucket, not the raw
        `blur_score` - a barely-sharp photo with zero faces still beats a
        blurry photo with many faces, because bucket membership is
        evaluated before anything else (design §3.3)."""
        barely_sharp = _photo(
            status=PhotoStatus.done, blur_score=SHARPNESS_THRESHOLD + 1, faces_count=0
        )
        blurry_but_full_of_faces = _photo(
            status=PhotoStatus.done, blur_score=SHARPNESS_THRESHOLD - 1, faces_count=5
        )

        assert (
            select_best_photo([blurry_but_full_of_faces, barely_sharp]) == barely_sharp.photo_id
        )

    def test_more_faces_wins_within_the_same_sharpness_bucket(self):
        few = _photo(status=PhotoStatus.done, blur_score=SHARP_SCORE, faces_count=1)
        many = _photo(status=PhotoStatus.done, blur_score=SHARP_SCORE, faces_count=5)

        assert select_best_photo([few, many]) == many.photo_id

    def test_fewer_closed_eyes_wins_when_bucket_and_faces_tied(self):
        eyes_open = _photo(
            status=PhotoStatus.done, blur_score=SHARP_SCORE, faces_count=2, eyes_closed_count=0
        )
        eyes_closed = _photo(
            status=PhotoStatus.done, blur_score=SHARP_SCORE, faces_count=2, eyes_closed_count=2
        )

        assert select_best_photo([eyes_closed, eyes_open]) == eyes_open.photo_id

    def test_null_eyes_closed_count_is_treated_as_zero(self):
        """Pre-v003 rows (or an analyzer that didn't fill the field) have
        `eyes_closed_count = NULL` - treated as "no closed eyes", not
        penalized against an explicit `0` (design §3.3)."""
        null_eyes = _photo(
            status=PhotoStatus.done, blur_score=SHARP_SCORE, faces_count=2, eyes_closed_count=None
        )
        one_closed_eye = _photo(
            status=PhotoStatus.done, blur_score=SHARP_SCORE, faces_count=2, eyes_closed_count=1
        )

        assert select_best_photo([one_closed_eye, null_eyes]) == null_eyes.photo_id

    def test_higher_blur_score_wins_within_same_sharpness_bucket(self):
        """TASK-003 A15: direction flipped from the old ASC (lower-is-
        sharper) to DESC (higher-is-sharper), matching the real analyzer's
        Laplacian-variance semantics (spike A3 finding 3)."""
        less_sharp = _photo(
            status=PhotoStatus.done, blur_score=SHARPNESS_THRESHOLD + 1, faces_count=1
        )
        more_sharp = _photo(
            status=PhotoStatus.done, blur_score=SHARP_SCORE, faces_count=1
        )

        assert select_best_photo([less_sharp, more_sharp]) == more_sharp.photo_id

    def test_earlier_created_at_wins_when_everything_else_tied(self):
        earlier = _photo(
            status=PhotoStatus.done,
            blur_score=SHARP_SCORE,
            faces_count=2,
            created_at=BASE_TIME,
        )
        later = _photo(
            status=PhotoStatus.done,
            blur_score=SHARP_SCORE,
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
            blur_score=SHARP_SCORE,
            faces_count=2,
            created_at=BASE_TIME,
            photo_id=id_a,
        )
        b = _photo(
            status=PhotoStatus.done,
            blur_score=SHARP_SCORE,
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
        repository.try_complete.assert_not_called()

    async def test_completed_status_with_best_photo_id_when_all_terminal(self):
        """TASK-002.1 F3: `get_batch` is strictly read-only - it computes
        `completed`/`best_photo_id` for display but must not write them
        through (that now happens in the worker, see
        `AnalysisProcessor._maybe_complete_batch` /
        `BatchRepository.try_complete`)."""
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
        repository.try_complete.assert_not_called()
        session.commit.assert_not_called()

    async def test_completed_with_all_failed_yields_null_best_photo_id(self):
        photos = [_photo(status=PhotoStatus.failed), _photo(status=PhotoStatus.failed)]
        batch = _batch(photos)
        repository = AsyncMock()
        repository.get_by_id.return_value = batch
        session = AsyncMock()
        service = BatchService(batch_repository=repository)

        result = await service.get_batch(session=session, batch_id=batch.batch_id)

        assert result.status == "completed"
        assert result.best_photo_id is None
        repository.try_complete.assert_not_called()
        session.commit.assert_not_called()

    async def test_already_completed_batch_does_not_write_through_again(self):
        """Idempotency: a GET of an already-`completed` batch must not
        write anything either - `get_batch` never writes, regardless of
        the batch's current persisted status (design §11 risk #11, F3)."""
        best = _photo(status=PhotoStatus.done, is_blurred=False, blur_score=0.1, faces_count=3)
        batch = _batch([best], status="completed")
        repository = AsyncMock()
        repository.get_by_id.return_value = batch
        session = AsyncMock()
        service = BatchService(batch_repository=repository)

        result = await service.get_batch(session=session, batch_id=batch.batch_id)

        assert result.status == "completed"
        assert result.best_photo_id == str(best.photo_id)
        repository.try_complete.assert_not_called()
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
