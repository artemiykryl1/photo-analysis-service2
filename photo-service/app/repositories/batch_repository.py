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

    async def mark_completed(
        self, session: AsyncSession, batch_id: uuid.UUID, best_photo_id: uuid.UUID | None
    ) -> None:
        """Write-through the computed `best_photo_id` once all photos in
        the batch reach a terminal status (design §5, §11 risk #11).
        Idempotent - the formula is deterministic, so re-running this with
        the same inputs is a no-op in effect.
        """
        await session.execute(
            update(Batch)
            .where(Batch.batch_id == batch_id)
            .values(status="completed", best_photo_id=best_photo_id)
        )
