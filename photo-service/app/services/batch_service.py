"""Business logic for the batches domain: fetching a batch's aggregate view
and picking its "best" photo.

`select_best_photo` is a **pure function** (no I/O, no session, no
`Settings`) - it only looks at already-loaded `Photo` ORM instances (with
`.analysis` populated or `None`), so it is trivially unit-testable without
a database (design §12 step 22, §13.2). Staying free of `Settings` is not
incidental - `SHARPNESS_THRESHOLD` below is a module constant, not an env
var, specifically so this function's purity (and therefore its testability
without app configuration) is preserved (TASK-003 A15).

ЗАФИКСИРОВАНО (redacted by TASK-003, tasks/feature-upload/tasks.md
"Лучший кадр батча", design §3): sort key over `done` photos with a
non-null analysis result is

    1. sharp_enough DESC     - blur_score >= SHARPNESS_THRESHOLD
    2. faces_count DESC
    3. eyes_closed_count ASC (NULL treated as 0)
    4. blur_score DESC       - exact sharpness within the bucket
    5. created_at ASC
    6. photo_id ASC          - final deterministic tie-break

No `done` photo (or none with a result) -> `None`.

TASK-003 A15/spike A3 (tasks/TASK-003/05_spike_analyzer.md finding 3): the
real analyzer's `blur_score` is a Laplacian variance where HIGHER means
SHARPER (range observed: 0.4-2.9 for blurry frames, 930-99 774 for sharp
ones) - the OPPOSITE of the old stub's 0..1 "lower is sharper" scale this
formula used to sort by (`blur_score ASC` as the near-first key). Left
unchanged, the formula would have picked the BLURRIEST frame of every
batch - the exact opposite of the feature's purpose. Two changes follow
from that finding:

1. **Direction flipped**: `blur_score` is now a DESCENDING key (bigger is
   better), not ascending.
2. **A bucket, not the raw value, comes first**: sorting by raw
   `blur_score DESC` as the very first key would make every other
   criterion dead code in practice - a continuous, effectively-never-tied
   value would almost always decide the winner outright, so "more faces"
   or "fewer closed eyes" could never overturn a 3%-sharper photo. The
   `sharp_enough` bucket (`>= SHARPNESS_THRESHOLD`) restores meaning to the
   later keys: first reject objectively unusable (blurry) frames, THEN
   choose among the rest by content (faces, eyes), and only fall back to
   exact sharpness as a fine-grained tie-break within the bucket.

`is_blurred` is DELIBERATELY NOT part of this formula (design §3.4): spike
A3 measured it as `True` for every single sample, including the sharpest
one (`blur_score = 99 774`), so as a sort key it cannot distinguish
anything and would make every candidate tie on the first comparison. Using
it would also make our main feature's behavior implicitly depend on
whether the analyzer author ever fixes that field - an unwanted external
dependency in either direction. The field is still stored and returned in
the API for transparency (`app.schemas.photos.AnalysisResultResponse`);
only the sort key ignores it.

Review-1 fix (tasks/TASK-002/40_review-1.md M1): `created_at` is a no-op
tie-breaker in practice - all photos of a batch are created inside the
same `create_batch` transaction and therefore share the same PostgreSQL
`now()`. `photo_id` is appended as a final, always-distinct tie-breaker so
the result is fully deterministic even on a complete tie of every other
field, instead of depending on `batch.photos` iteration order. This
invariant is unchanged by TASK-003.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import Photo, PhotoStatus
from app.repositories.batch_repository import BatchRepository
from app.schemas.photos import BatchPhotoDetail, BatchResponse
from app.services.mappers import analysis_to_response

_TERMINAL_STATUSES = frozenset({PhotoStatus.done, PhotoStatus.failed})

# TASK-003 A15 (tasks/TASK-003/20_design.md §3.3): midpoint (on a log
# scale) between the spike's blurry measurements (0.375-1.10) and sharp
# measurements (930.47-99 774) - comfortably separates both groups with
# margin on either side. A module constant (not `Settings`) so
# `select_best_photo` stays a pure, dependency-free function - see module
# docstring.
SHARPNESS_THRESHOLD = 100.0


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
            not (p.analysis.blur_score >= SHARPNESS_THRESHOLD),  # False (sharp) sorts first
            -p.analysis.faces_count,
            p.analysis.eyes_closed_count or 0,  # NULL (pre-v003 rows) treated as 0
            -p.analysis.blur_score,
            p.created_at,
            p.photo_id,
        ),
    )
    return best.photo_id


class BatchService:
    def __init__(self, batch_repository: BatchRepository) -> None:
        self._repository = batch_repository

    async def get_batch(self, session: AsyncSession, batch_id: uuid.UUID) -> BatchResponse:
        """Fetch a batch and derive its aggregate `status`/`best_photo_id`
        from the current state of its photos - strictly read-only.

        TASK-002.1 (tasks/TASK-002.1/20_design.md F3): this method used to
        write the computed `completed`/`best_photo_id` through to the
        `batches` row on the first GET that observed all photos terminal.
        That made a supposedly-safe GET perform an `UPDATE` + `commit`
        (concurrent GETs racing each other, retried/duplicated proxy
        requests writing to the DB, and the batch never completing at all
        if nobody happened to call GET). The actual persistence of
        `completed`/`best_photo_id` now happens in the worker
        (`AnalysisProcessor._maybe_complete_batch`, via the atomic
        `BatchRepository.try_complete`) right after each photo's terminal
        write. This method only *computes* the same values for display -
        it does not touch `batch.status`/`batch.best_photo_id` in the DB,
        so the response looks identical to a client regardless of whether
        the worker has already persisted the completion or not.
        """
        batch = await self._repository.get_by_id(session, batch_id)
        if batch is None:
            raise NotFoundError("Batch not found")

        photos = batch.photos
        all_terminal = bool(photos) and all(p.status in _TERMINAL_STATUSES for p in photos)

        if all_terminal:
            status = "completed"
            best_photo_id = select_best_photo(photos)  # display-only, never persisted here
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
                    analysis=analysis_to_response(p.analysis),
                )
                for p in photos
            ],
            best_photo_id=str(best_photo_id) if best_photo_id is not None else None,
        )
