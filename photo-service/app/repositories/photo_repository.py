"""Data access layer for the `photos` table.

`PhotoRepository` methods receive an `AsyncSession` as an argument - the
repository never creates its own session and never commits globally; the
transaction boundary is owned by the calling layer (services/). SQL lives
ONLY here - no other module may import `sqlalchemy.select`/execute a query
against `Photo`.

TASK-002 (tasks/TASK-002/20_design.md §5, §9): adds the worker/outbox
methods (`claim_for_processing`, `record_attempt`, `mark_done`,
`mark_failed`, `fetch_unpublished`, `mark_published`) and eager-loads
`Photo.analysis` in `get_by_id`/`list` via `selectinload` - the ORM
relationship must not be lazy-loaded from async code.

`from __future__ import annotations` (below) makes all annotations in
this module lazy strings (PEP 563), regardless of Python version. This
is required because the class defines a method named `list` (line ~53)
which shadows the builtin `list` inside the class namespace for every
annotation evaluated afterwards in the class body (e.g. `-> list[Photo]`
on `fetch_unpublished`). Without deferred evaluation, eager annotation
resolution (the default on Python <3.14, notably the `python:3.12-slim`
runtime image) raises `TypeError: 'function' object is not
subscriptable` at import time - see tasks/TASK-002/60_debug.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
        """Fetch a photo by its primary key, with its analysis result
        (if any) eager-loaded so `photo.analysis` never triggers a lazy
        load (design §9)."""
        result = await session.execute(
            select(Photo)
            .options(selectinload(Photo.analysis))
            .where(Photo.photo_id == photo_id)
        )
        return result.scalar_one_or_none()

    async def list(self, session: AsyncSession, limit: int, offset: int) -> list[Photo]:
        """List photos ordered by most recently created first, with each
        photo's analysis result eager-loaded (design §9)."""
        result = await session.execute(
            select(Photo)
            .options(selectinload(Photo.analysis))
            .order_by(Photo.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def update_status(
        self, session: AsyncSession, photo_id: uuid.UUID, status: PhotoStatus
    ) -> None:
        """Update a photo's status. Commit is the caller's responsibility."""
        await session.execute(
            update(Photo).where(Photo.photo_id == photo_id).values(status=status)
        )

    async def claim_for_processing(self, session: AsyncSession, photo_id: uuid.UUID) -> int:
        """Atomically transition `pending` -> `processing`.

        ЗАФИКСИРОВАНО (tasks/TASK-002/20_design.md §5.3, §12): the
        predicate `WHERE photo_id=:id AND status='pending'` must not
        change. Returns the number of rows updated: 1 = this worker won
        the claim, 0 = the photo is already claimed or terminal
        (duplicate Kafka delivery / rebalance replay) - caller must skip.
        """
        result = await session.execute(
            update(Photo)
            .where(Photo.photo_id == photo_id, Photo.status == PhotoStatus.pending)
            .values(status=PhotoStatus.processing)
        )
        return result.rowcount

    async def record_attempt(
        self,
        session: AsyncSession,
        photo_id: uuid.UUID,
        attempts: int,
        error_code: str,
        error_message: str,
    ) -> None:
        """Persist progress after a failed analyzer attempt (design §5.3
        step 2) - so `attempts`/`last_error_*` reflect reality even if the
        worker crashes before a terminal decision is reached."""
        await session.execute(
            update(Photo)
            .where(Photo.photo_id == photo_id)
            .values(
                attempts=attempts, last_error_code=error_code, last_error_message=error_message
            )
        )

    async def mark_done(self, session: AsyncSession, photo_id: uuid.UUID, attempts: int) -> None:
        """Terminal success: `status='done'` (design §5.3 step 3).

        Review-1 fix (tasks/TASK-002/40_review-1.md M3): also clears
        `last_error_code`/`last_error_message` - a photo that failed on
        attempt 1 (retryable) and then succeeded on attempt 2 would
        otherwise end up `status='done'` while still carrying the stale
        error from the earlier attempt, which is misleading when
        inspecting the row.
        """
        await session.execute(
            update(Photo)
            .where(Photo.photo_id == photo_id)
            .values(
                status=PhotoStatus.done,
                attempts=attempts,
                last_error_code=None,
                last_error_message=None,
            )
        )

    async def mark_failed(
        self,
        session: AsyncSession,
        photo_id: uuid.UUID,
        error_code: str,
        error_message: str,
        attempts: int,
    ) -> None:
        """Terminal failure: `status='failed'` + `last_error_*` (design
        §5.3 step 3)."""
        await session.execute(
            update(Photo)
            .where(Photo.photo_id == photo_id)
            .values(
                status=PhotoStatus.failed,
                last_error_code=error_code,
                last_error_message=error_message,
                attempts=attempts,
            )
        )

    async def fetch_unpublished(self, session: AsyncSession, limit: int) -> list[Photo]:
        """Outbox poll: rows still `publish_status='not_sent'` (design §4.2).

        TASK-003 review-1 MAJOR-1: `api` runs with `replicas: 2` in
        `k8s/31-api.yaml`, and `run_outbox_publisher` starts once per
        replica (`app/main.py` lifespan) - without row-level locking, two
        replicas polling in the same window would both select the same
        `not_sent` rows and publish each of them twice to Kafka (the worker
        stays correct either way thanks to its atomic claim + idempotent
        upsert, but it doubles load on the shared external analyzer, which
        several other students also depend on). `FOR UPDATE SKIP LOCKED`
        makes this safe for any number of replicas: the caller
        (`run_outbox_publisher`) holds this transaction open across the
        whole batch and only commits (releasing the row locks) after
        `mark_published`/skip-and-retry has been decided for every row it
        selected, so a concurrent poller skips these rows entirely instead
        of re-selecting them.
        """
        result = await session.execute(
            select(Photo)
            .where(Photo.publish_status == "not_sent")
            .order_by(Photo.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result.scalars().all())

    async def mark_published(self, session: AsyncSession, photo_id: uuid.UUID) -> None:
        """Outbox success: `publish_status='sent'` + `published_at=now()`
        (design §4.2)."""
        await session.execute(
            update(Photo)
            .where(Photo.photo_id == photo_id)
            .values(publish_status="sent", published_at=datetime.now(timezone.utc))
        )

    async def get_batch_id(self, session: AsyncSession, photo_id: uuid.UUID) -> uuid.UUID | None:
        """`SELECT batch_id FROM photos WHERE photo_id=:id` - used by the
        worker (`AnalysisProcessor._maybe_complete_batch`, design §F3) to
        decide whether a just-terminated photo belongs to a batch that may
        now be completable. Returns `None` both when the photo has no
        batch (single-photo upload - the common case, zero extra queries
        beyond this one) and when the photo id itself doesn't exist."""
        result = await session.execute(
            select(Photo.batch_id).where(Photo.photo_id == photo_id)
        )
        return result.scalar_one_or_none()

    async def count_pending(self, session: AsyncSession) -> int:
        """`SELECT count(*) WHERE status='pending'` - backs the
        `photos_pending` gauge, refreshed on every `/metrics` scrape
        (design §8.4)."""
        result = await session.execute(
            select(func.count()).select_from(Photo).where(Photo.status == PhotoStatus.pending)
        )
        return result.scalar_one()
