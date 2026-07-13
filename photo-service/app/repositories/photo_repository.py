"""Data access layer for the `photos` table.

`PhotoRepository` methods receive an `AsyncSession` as an argument - the
repository never creates its own session and never commits globally; the
transaction boundary is owned by the calling layer (services/).

TASK-000 scope: skeleton only, no working queries. Real implementation
lands in TASK-001.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Photo


class PhotoRepository:
    async def create(self, session: AsyncSession, photo: Photo) -> Photo:
        """Insert a new photo row.

        TODO(TASK-001): implement (session.add + flush; conflict handling
        maps to ConflictError per spec §2).
        """
        raise NotImplementedError("create will be implemented in TASK-001")

    async def get(self, session: AsyncSession, photo_id: uuid.UUID) -> Photo | None:
        """Fetch a photo by id.

        TODO(TASK-001/TASK-004): implement (select by primary key).
        """
        raise NotImplementedError("get will be implemented in TASK-001")
