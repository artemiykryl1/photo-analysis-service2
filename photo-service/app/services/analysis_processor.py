"""Domain logic for processing a single `photo.analysis.requested` message.

Shared by `app.worker.consumer` (real Kafka messages) and unit tests (no
Kafka connection needed) - the atomic claim -> gRPC retry loop -> terminal
write sequence from tasks/TASK-002/20_design.md §5.3 lives here so it can
be tested independently of `aiokafka`.

Must not know about Kafka (`aiokafka`) or ASGI - callers own the
`AsyncSession` and the message payload; this class only orchestrates
`repositories/` + the gRPC client.

TASK-002.1 (tasks/TASK-002.1/20_design.md F3): after every terminal write
(a duplicate-skip, a `done`, or a `failed`), this class also checks
whether the photo belongs to a batch whose photos are now ALL terminal,
and - if so - completes the batch atomically. This replaces the old
`BatchService.get_batch`-writes-on-GET design (F3 problem: a read
endpoint performing an `UPDATE` + `commit`). Importing `select_best_photo`
from `app.services.batch_service` (not `app.services.photo_service`) is
deliberate - `batch_service` does not import `photo_service`, so this
module's import graph never pulls in `app.integrations.metrics_api`
(API-only metrics) - see the F4 import-hygiene invariant.
"""

import asyncio
import logging
import time
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import PhotoStatus
from app.integrations.analyzer_client import (
    AnalyzerGrpcClient,
    RetryDecision,
    classify_grpc_error,
    error_code_from_exception,
    truncate_error_message,
)
from app.integrations.metrics_worker import (
    analyzer_grpc_errors_total,
    photo_analysis_completed_total,
    photo_analysis_duration_seconds,
    photo_analysis_failed_total,
    photo_analysis_started_total,
    worker_messages_processed_total,
)
from app.repositories.analysis_result_repository import AnalysisResultRepository
from app.repositories.batch_repository import BatchRepository
from app.repositories.photo_repository import PhotoRepository
from app.services.batch_service import select_best_photo

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({PhotoStatus.done, PhotoStatus.failed})


class AnalysisProcessor:
    """Claim -> analyze (with bounded retry) -> persist, for one photo."""

    def __init__(
        self,
        photo_repository: PhotoRepository,
        analysis_repository: AnalysisResultRepository,
        analyzer: AnalyzerGrpcClient,
        settings: Settings,
        batch_repository: BatchRepository | None = None,
    ) -> None:
        self._photos = photo_repository
        self._analysis = analysis_repository
        self._analyzer = analyzer
        self._max_attempts = settings.WORKER_MAX_ATTEMPTS
        self._backoff_base = settings.RETRY_BACKOFF_BASE_SECONDS
        self._grpc_timeout = settings.ANALYZER_GRPC_TIMEOUT
        self._batches = batch_repository or BatchRepository()

    async def _maybe_complete_batch(self, session: AsyncSession, photo_id: uuid.UUID) -> None:
        """If `photo_id` belongs to a batch and every photo in that batch
        is now in a terminal status (`done`/`failed`), atomically complete
        the batch (design §F3). No-op for single-photo uploads
        (`get_batch_id` returns `None`) and for batches already
        `completed` or not yet fully terminal.
        """
        batch_id = await self._photos.get_batch_id(session, photo_id)
        if batch_id is None:
            return

        batch = await self._batches.get_by_id(session, batch_id)
        if batch is None or batch.status != "processing":
            return

        photos = batch.photos
        if not (photos and all(p.status in _TERMINAL_STATUSES for p in photos)):
            return

        best_photo_id = select_best_photo(photos)  # pure function, unchanged by F3
        rowcount = await self._batches.try_complete(session, batch_id, best_photo_id)
        await session.commit()
        if rowcount:
            logger.info(
                "batch completed",
                extra={
                    "batch_id": str(batch_id),
                    "best_photo_id": str(best_photo_id) if best_photo_id else None,
                },
            )

    async def process(self, session: AsyncSession, photo_id: str, object_key: str) -> None:
        """tasks/TASK-002/20_design.md §5.3. `session` must be fresh (one
        session per message, opened/closed by the caller - see §5.2)."""
        photo_uuid = uuid.UUID(photo_id)

        # Step 1: atomic claim (ЗАФИКСИРОВАНО predicate - see photo_repository).
        rowcount = await self._photos.claim_for_processing(session, photo_uuid)
        await session.commit()

        if rowcount == 0:
            logger.info(
                "skip duplicate: photo already claimed or terminal",
                extra={"photo_id": photo_id},
            )
            # F3 self-healing: a redelivered message for an already-terminal
            # photo is exactly the case where a prior delivery committed the
            # terminal write but crashed/lost its offset before completing
            # the batch check below - retry it here too.
            await self._maybe_complete_batch(session, photo_uuid)
            # TASK-002.1 review-1 m4: increment only after `_maybe_complete_batch`
            # returns without raising, so exactly one increment corresponds to
            # one fully-processed message (if it raises, the message is
            # redelivered and must be free to retry this branch again without
            # double-counting). `worker_messages_processed_total` counts
            # *messages* (per-delivery outcome), unlike `photo_analysis_
            # completed_total`/`photo_analysis_failed_total` below, which count
            # *photos* (per-photo terminal outcome) and are intentionally left
            # where they were.
            worker_messages_processed_total.labels(result="skipped").inc()
            return

        photo_analysis_started_total.inc()
        t0 = time.monotonic()
        logger.info("analysis claim won", extra={"photo_id": photo_id})

        # Step 2: in-process retry loop (backoff 1,2,4s; limit WORKER_MAX_ATTEMPTS).
        attempts = 0
        last_code = "UNKNOWN"
        last_message = ""
        decision = RetryDecision.NO_RETRY
        response = None

        for attempt in range(1, self._max_attempts + 1):
            attempts = attempt
            try:
                async with asyncio.timeout(self._grpc_timeout):
                    response = await self._analyzer.analyze(photo_id, object_key)
                break
            except Exception as exc:  # noqa: BLE001 - classified immediately below, never swallowed
                decision = classify_grpc_error(exc)
                last_code = error_code_from_exception(exc)
                last_message = truncate_error_message(str(exc))
                analyzer_grpc_errors_total.labels(code=last_code).inc()
                logger.warning(
                    "analyzer gRPC call failed",
                    extra={
                        "photo_id": photo_id,
                        "attempt": attempt,
                        "error_code": last_code,
                        "decision": decision.value,
                    },
                )
                await self._photos.record_attempt(
                    session, photo_uuid, attempts, last_code, last_message
                )
                await session.commit()

                if decision == RetryDecision.NO_RETRY or attempt == self._max_attempts:
                    response = None
                    break

                await asyncio.sleep(self._backoff_base * (2 ** (attempt - 1)))

        duration_seconds = time.monotonic() - t0
        photo_analysis_duration_seconds.observe(duration_seconds)

        # Step 3: terminal write (one transaction).
        if response is not None:
            await self._analysis.upsert(
                session,
                photo_uuid,
                faces_count=response.faces_count,
                is_blurred=response.is_blurred,
                blur_score=response.blur_score,
                perceptual_hash=response.perceptual_hash,
            )
            await self._photos.mark_done(session, photo_uuid, attempts)
            await session.commit()
            photo_analysis_completed_total.inc()
            logger.info(
                "analysis completed",
                extra={"photo_id": photo_id, "attempt": attempts},
            )
            await self._maybe_complete_batch(session, photo_uuid)
            # review-1 m4: single per-message increment, after the batch
            # check succeeds - see the comment on the skip-path increment above.
            worker_messages_processed_total.labels(result="done").inc()
            return

        reason = "no_retry" if decision == RetryDecision.NO_RETRY else "retries_exhausted"
        await self._photos.mark_failed(session, photo_uuid, last_code, last_message, attempts)
        await session.commit()
        photo_analysis_failed_total.labels(reason=reason).inc()
        logger.error(
            "analysis failed",
            extra={
                "photo_id": photo_id,
                "attempt": attempts,
                "reason": reason,
                "error_code": last_code,
            },
        )
        await self._maybe_complete_batch(session, photo_uuid)
        # review-1 m4: single per-message increment, after the batch check
        # succeeds - see the comment on the skip-path increment above.
        worker_messages_processed_total.labels(result="failed").inc()
