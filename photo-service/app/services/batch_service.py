"""Business logic for the batches domain: fetching a batch's aggregate view
and picking its "best" photo.

`select_best_photo` is a **pure function** (no I/O, no session) - it only
looks at already-loaded `Photo` ORM instances (with `.analysis` populated
or `None`), so it is trivially unit-testable without a database (design
§12 step 22, §13.2).

ЗАФИКСИРОВАНО (tasks/TASK-002/20_design.md §12 Приложение): sort key is
`is_blurred ASC, blur_score ASC, faces_count DESC, created_at ASC`, over
`done` photos only; no `done` photo -> `None`.

Review-1 fix (tasks/TASK-002/40_review-1.md M1): `created_at` is a no-op
tie-breaker in practice - all photos of a batch are created inside the
same `create_batch` transaction and therefore share the same PostgreSQL
`now()`. `photo_id` is appended as a final, always-distinct tie-breaker so
the result is fully deterministic even on a complete tie of every other
field, instead of depending on `batch.photos` iteration order.
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import Photo, PhotoStatus
from app.repositories.batch_repository import BatchRepository
from app.schemas.photos import BatchPhotoDetail, BatchResponse
from app.services.photo_service import _analysis_response

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({PhotoStatus.done, PhotoStatus.failed})


def select_best_photo(photos: list[Photo]) -> uuid.UUID | None:
    """Pick the best `done` photo of a batch, or `None` if none is `done`.

    Only photos with `status == done` (and therefore an `analysis` result)
    are considered - `pending`/`processing`/`failed` photos are excluded.
    """
    candidates = [p for p in photos if p.status == PhotoStatus.done and p.analysis is not None]
    if not candidates:
        return None

    best = min(
        candidates,
        key=lambda p: (
            p.analysis.is_blurred,
            p.analysis.blur_score,
            -p.analysis.faces_count,
            p.created_at,
            p.photo_id,
        ),
    )
    return best.photo_id


class BatchService:
    def __init__(self, batch_repository: BatchRepository) -> None:
        self._repository = batch_repository

    async def get_batch(self, session: AsyncSession, batch_id: uuid.UUID) -> BatchResponse:
        """Fetch a batch, derive its aggregate `status`/`best_photo_id`
        from the current state of its photos, and (on first observed
        completion) persist that computation (design §11 risk #5, #11)."""
        batch = await self._repository.get_by_id(session, batch_id)
        if batch is None:
            raise NotFoundError("Batch not found")

        photos = batch.photos
        all_terminal = bool(photos) and all(p.status in _TERMINAL_STATUSES for p in photos)

        if all_terminal:
            status = "completed"
            best_photo_id = select_best_photo(photos)
            if batch.status != "completed":
                await self._repository.mark_completed(session, batch.batch_id, best_photo_id)
                await session.commit()
                logger.info(
                    "batch completed",
                    extra={"batch_id": str(batch.batch_id), "best_photo_id": str(best_photo_id)},
                )
        else:
            status = "processing"
            best_photo_id = None

        return BatchResponse(
            batch_id=str(batch.batch_id),
            status=status,
            photos=[
                BatchPhotoDetail(
                    photo_id=str(p.photo_id),
                    filename=p.filename,
                    status=p.status.value,
                    analysis=_analysis_response(p.analysis),
                )
                for p in photos
            ],
            best_photo_id=str(best_photo_id) if best_photo_id is not None else None,
        )
