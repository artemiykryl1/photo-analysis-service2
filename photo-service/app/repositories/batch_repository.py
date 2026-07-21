"""Data access layer for the `batches` table.

Same rule as `photo_repository.py`: `BatchRepository` never owns the
transaction (no internal commits) and never creates its own session - the
calling layer (`services/batch_service.py`, `services/photo_service.py`)
owns commit/rollback. SQL for batches lives ONLY here.

tasks/TASK-002/20_design.md §12 step 20.
"""

import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Batch, Photo


class BatchRepository:
    async def create(self, session: AsyncSession, batch: Batch) -> Batch:
        """Insert a new batch row (add + flush, no commit)."""
        session.add(batch)
        await session.flush()
        return batch

    async def get_by_id(self, session: AsyncSession, batch_id: uuid.UUID) -> Batch | None:
        """Fetch a batch with its photos (and each photo's analysis
        result) eager-loaded, so `batch.photos[i].analysis` never triggers
        a lazy load from async code (design §9/§22)."""
        result = await session.get(
            Batch,
            batch_id,
            options=[selectinload(Batch.photos).selectinload(Photo.analysis)],
        )
        return result

    async def try_complete(
        self, session: AsyncSession, batch_id: uuid.UUID, best_photo_id: uuid.UUID | None
    ) -> int:
        """Atomically transition a batch `processing` -> `completed`.

        TASK-002.1 (tasks/TASK-002.1/20_design.md F3): replaces the old
        unconditional `mark_completed` - the predicate
        `WHERE batch_id=:id AND status='processing'` makes this call safe
        to race: if two worker instances both finish the last two photos
        of the same batch at nearly the same time, both may compute the
        same deterministic `best_photo_id` and call this method, but only
        the first UPDATE actually matches a row (rowcount=1); the second
        finds the row already `completed` and matches nothing (rowcount=0)
        - a harmless no-op, not a duplicate/racing write. Commit is the
        caller's responsibility (this method never commits itself).
        """
        result = await session.execute(
            update(Batch)
            .where(Batch.batch_id == batch_id, Batch.status == "processing")
            .values(status="completed", best_photo_id=best_photo_id)
        )
        return result.rowcount
