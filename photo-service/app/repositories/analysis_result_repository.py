"""Data access layer for the `analysis_results` table.

TASK-002 (tasks/TASK-002/20_design.md §2.1, §5.3): 1:1 result per photo.
`upsert` uses `INSERT ... ON CONFLICT (photo_id) DO NOTHING` so that a
re-delivered Kafka message (at-least-once) reaching the terminal-write step
twice never overwrites an existing result or raises an `IntegrityError` -
`photo_id` is both the primary key and the idempotency key.

TASK-003 A6/A12 (tasks/TASK-003/20_design.md §6.4): explicitly kept
`ON CONFLICT DO NOTHING`, not `DO UPDATE`, even though it means a photo's
new (v003) fields can never be backfilled onto a result row written by an
older delivery. `DO NOTHING` IS the idempotency guarantee from TASK-002 -
switching to `DO UPDATE` would let a redelivered message overwrite an
existing result with data from a different attempt (e.g. a different
downscale/compression pass), which is exactly the kind of
non-determinism at-least-once delivery is supposed to be safe against.
"""

import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AnalysisResult


class AnalysisResultRepository:
    async def upsert(
        self,
        session: AsyncSession,
        photo_id: uuid.UUID,
        faces_count: int,
        is_blurred: bool,
        blur_score: float,
        perceptual_hash: str,
        eyes_closed_count: int | None = None,
        dominant_color: str | None = None,
        tags: list[str] | None = None,
        model_version: str | None = None,
    ) -> None:
        """Insert the analyzer result; a no-op if a result for this
        `photo_id` already exists (idempotent write, see module docstring).
        """
        stmt = pg_insert(AnalysisResult).values(
            photo_id=photo_id,
            faces_count=faces_count,
            is_blurred=is_blurred,
            blur_score=blur_score,
            perceptual_hash=perceptual_hash,
            eyes_closed_count=eyes_closed_count,
            dominant_color=dominant_color,
            tags=tags,
            model_version=model_version,
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=[AnalysisResult.photo_id])
        await session.execute(stmt)
