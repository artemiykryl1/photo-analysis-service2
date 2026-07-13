"""Data access layer for the `photos` table.

`PhotoRepository` methods receive an `AsyncSession` as an argument - the
repository never creates its own session and never commits globally; the
transaction boundary is owned by the calling layer (services/). SQL lives
ONLY here - no other module may import `sqlalchemy.select`/execute a query
against `Photo`.
"""

import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.db.models import Photo, PhotoStatus


class PhotoRepository:
    async def create(self, session: AsyncSession, photo: Photo) -> Photo:
        """Insert a new photo row (add + flush, no commit).

        Raises `ConflictError` if the insert violates a uniqueness
        constraint (PK `photo_id` or UNIQUE `object_key`).
        """
        session.add(photo)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise ConflictError("Photo already exists") from exc
        return photo

    async def get_by_id(self, session: AsyncSession, photo_id: uuid.UUID) -> Photo | None:
        """Fetch a photo by its primary key."""
        result = await session.execute(select(Photo).where(Photo.photo_id == photo_id))
        return result.scalar_one_or_none()

    async def list(self, session: AsyncSession, limit: int, offset: int) -> list[Photo]:
        """List photos ordered by most recently created first."""
        result = await session.execute(
            select(Photo).order_by(Photo.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def update_status(
        self, session: AsyncSession, photo_id: uuid.UUID, status: PhotoStatus
    ) -> None:
        """Update a photo's status. Commit is the caller's responsibility.

        For future use by the Kafka result consumer (TASK-002/TASK-003).
        """
        await session.execute(
            update(Photo).where(Photo.photo_id == photo_id).values(status=status)
        )
