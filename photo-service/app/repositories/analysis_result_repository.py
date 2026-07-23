"""Data access layer for the `analysis_results` table.

TASK-002 (tasks/TASK-002/20_design.md §2.1, §5.3): 1:1 result per photo.
`upsert` uses `INSERT ... ON CONFLICT (photo_id) DO NOTHING` so that a
re-delivered Kafka message (at-least-once) reaching the terminal-write step
twice never overwrites an existing result or raises an `IntegrityError` -
`photo_id` is both the primary key and the idempotency key.
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
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=[AnalysisResult.photo_id])
        await session.execute(stmt)
