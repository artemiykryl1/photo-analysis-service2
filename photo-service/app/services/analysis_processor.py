"""Domain logic for processing a single `photo.analysis.requested` message.

Shared by `app.worker.consumer` (real Kafka messages) and unit tests (no
Kafka connection needed) - the atomic claim -> gRPC retry loop -> terminal
write sequence from tasks/TASK-002/20_design.md §5.3 lives here so it can
be tested independently of `aiokafka`.

Must not know about Kafka (`aiokafka`) or ASGI - callers own the
`AsyncSession` and the message payload; this class only orchestrates
`repositories/` + the gRPC client (+ MinIO reads + image preparation, as of
TASK-003).

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

TASK-003 A2/A8/A9 (tasks/TASK-003/20_design.md §5): the worker now reads
the photo's bytes from MinIO and downscales them (if needed) BEFORE
calling the real analyzer, which requires the actual `image_bytes` and
hard-caps its gRPC message at 4 MiB (spike A3,
tasks/TASK-003/05_spike_analyzer.md). The download+prepare step happens
INSIDE the retry loop (after the claim, per §5.2 - no point downloading up
to 50 MB just to discard it in the skip branch) but is cached across
attempts (`if prepared is None`) so a transient MinIO/gRPC failure never
re-downloads or re-encodes bytes it already successfully prepared.
"""

import asyncio
import logging
import time
import uuid

import anyio.to_thread
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import PhotoStatus
from app.integrations.analyzer_client import (
    AnalyzerGrpcClient,
    AnalyzerMessageTooLarge,
    RetryDecision,
    truncate_error_message,
)
from app.integrations.metrics_worker import (
    analyzer_grpc_errors_total,
    analyzer_image_downscaled_total,
    analyzer_image_prepared_bytes,
    photo_analysis_completed_total,
    photo_analysis_duration_seconds,
    photo_analysis_failed_total,
    photo_analysis_started_total,
    storage_read_errors_total,
    worker_messages_processed_total,
)
from app.integrations.storage import ObjectStorage
from app.repositories.analysis_result_repository import AnalysisResultRepository
from app.repositories.batch_repository import BatchRepository
from app.repositories.photo_repository import PhotoRepository
from app.services.analysis_errors import classify_error
from app.services.batch_service import select_best_photo
from app.services.image_prep import PreparedImage, prepare_for_analysis

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({PhotoStatus.done, PhotoStatus.failed})

# TASK-003 A7/D3 (design §4.1): deliberately generous overhead estimate for
# `photo_id` + `object_key` + protobuf framing (< 150 bytes measured) - the
# preventive check errs on the side of catching a too-large payload before
# ever calling the analyzer, rather than trimming this margin to the bone.
_MESSAGE_OVERHEAD_BYTES = 1024

# TASK-003 A8/§12 (design "вежливость к общему анализатору"): error codes
# after which the worker throttles itself before returning, so a lengthy
# analyzer outage doesn't turn into a hot-loop against a shared external
# service. Deliberately narrow (not e.g. RESOURCE_EXHAUSTED, which already
# gets its own retry loop with backoff) - this cooldown is specifically for
# "the analyzer seems to be down/unreachable", not "a single call was slow".
_COOLDOWN_ERROR_CODES = frozenset({"UNAVAILABLE", "DEADLINE_EXCEEDED"})

# `last_error_code`s produced by the storage (MinIO read) path -
# `app.services.analysis_errors.classify_error` - as opposed to the
# analyzer/gRPC path. Used only to route the failure to the right Prometheus
# counter (design A-8 step 5); the retry decision itself is unaffected.
_STORAGE_ERROR_CODES = frozenset({"OBJECT_NOT_FOUND", "STORAGE_UNAVAILABLE"})


class AnalysisProcessor:
    """Claim -> prepare image -> analyze (with bounded retry) -> persist,
    for one photo."""

    def __init__(
        self,
        photo_repository: PhotoRepository,
        analysis_repository: AnalysisResultRepository,
        analyzer: AnalyzerGrpcClient,
        settings: Settings,
        batch_repository: BatchRepository | None = None,
        *,
        storage: ObjectStorage,
    ) -> None:
        self._photos = photo_repository
        self._analysis = analysis_repository
        self._analyzer = analyzer
        self._settings = settings
        self._max_attempts = settings.WORKER_MAX_ATTEMPTS
        self._backoff_base = settings.RETRY_BACKOFF_BASE_SECONDS
        self._grpc_timeout = settings.ANALYZER_GRPC_TIMEOUT
        self._batches = batch_repository or BatchRepository()
        # TASK-003 A9/D4 (design §5.1): `storage` is a required keyword-only
        # argument on purpose (not `| None = None`) - a half-configured
        # processor that silently skipped reading the photo's bytes would
        # be a silent production outage, not a graceful degradation. Any
        # caller (including existing test fixtures) that omits it now gets
        # an immediate, explicit `TypeError` instead.
        self._storage = storage
        self._storage_read_timeout = settings.STORAGE_READ_TIMEOUT_SECONDS
        self._image_prep_timeout = settings.IMAGE_PREP_TIMEOUT_SECONDS
        self._max_message_bytes = settings.ANALYZER_MAX_MESSAGE_BYTES
        self._cooldown_seconds = settings.ANALYZER_UNAVAILABLE_COOLDOWN_SECONDS

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

    async def _prepare_image(self, photo_id: str, object_key: str) -> PreparedImage:
        """Download the photo's bytes from MinIO and prepare them for the
        analyzer (TASK-003 A2/A5/D1, design §5.2-§5.3). Raises
        `NotFoundError`/`StorageUnavailable` (MinIO), `ImageDecodeError`/
        `ImageTooLargeError` (Pillow), or `AnalyzerMessageTooLarge` (the
        preventive size check) - all classified by
        `app.services.analysis_errors.classify_error` in the caller.
        """
        async with asyncio.timeout(self._storage_read_timeout):
            raw = await anyio.to_thread.run_sync(self._storage.get_file, object_key)
        original_bytes = len(raw)

        async with asyncio.timeout(self._image_prep_timeout):
            prepared = await anyio.to_thread.run_sync(
                prepare_for_analysis, raw, self._settings
            )
        raw = None  # release the original bytes as soon as we have the prepared copy

        if len(prepared.data) + _MESSAGE_OVERHEAD_BYTES > self._max_message_bytes:
            raise AnalyzerMessageTooLarge(len(prepared.data), self._max_message_bytes)

        # constitution.md §3.3: log sizes/dimensions, never the object_key
        # in full or any file content.
        logger.info(
            "image prepared",
            extra={
                "photo_id": photo_id,
                "original_bytes": original_bytes,
                "sent_bytes": len(prepared.data),
                "downscaled": prepared.downscaled,
                "width": prepared.width,
                "height": prepared.height,
            },
        )
        analyzer_image_prepared_bytes.observe(len(prepared.data))
        if prepared.downscaled:
            analyzer_image_downscaled_total.inc()

        return prepared

    async def process(self, session: AsyncSession, photo_id: str, object_key: str) -> None:
        """tasks/TASK-002/20_design.md §5.3, extended by TASK-003 A8
        (tasks/TASK-003/20_design.md §5.2). `session` must be fresh (one
        session per message, opened/closed by the caller)."""
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
        prepared: PreparedImage | None = None

        for attempt in range(1, self._max_attempts + 1):
            attempts = attempt
            try:
                if prepared is None:
                    # TASK-003 A2/D4 (design §5.2): download + prepare ONCE
                    # per message, cached across retry attempts - a
                    # transient MinIO or gRPC failure retries the same
                    # already-prepared bytes instead of re-downloading/
                    # re-encoding on every attempt.
                    prepared = await self._prepare_image(photo_id, object_key)

                async with asyncio.timeout(self._grpc_timeout):
                    response = await self._analyzer.analyze(
                        photo_id, object_key, prepared.data
                    )
                break
            except Exception as exc:  # noqa: BLE001 - classified immediately below, never swallowed
                decision, last_code = classify_error(exc)
                last_message = truncate_error_message(str(exc))
                if last_code in _STORAGE_ERROR_CODES:
                    storage_read_errors_total.labels(code=last_code).inc()
                else:
                    analyzer_grpc_errors_total.labels(code=last_code).inc()
                logger.warning(
                    "analysis attempt failed",
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

        if response is None and last_code in _COOLDOWN_ERROR_CODES:
            # TASK-003 A8/§12 (design "вежливость к общему анализатору"):
            # throttle before returning when the analyzer appears to be
            # down/unreachable, rather than letting the next Kafka message
            # immediately hammer it again. `asyncio.timeout` wrapping the
            # gRPC call already turns a hung call into `TimeoutError`
            # (code "TIMEOUT", not in this set) well before
            # ANALYZER_GRPC_TIMEOUT would itself act as a natural throttle.
            logger.warning(
                "analyzer unavailable, cooling down",
                extra={"photo_id": photo_id, "cooldown_seconds": self._cooldown_seconds},
            )
            await asyncio.sleep(self._cooldown_seconds)

        # Step 3: terminal write (one transaction).
        if response is not None:
            await self._analysis.upsert(
                session,
                photo_uuid,
                faces_count=response.faces_count,
                is_blurred=response.is_blurred,
                blur_score=response.blur_score,
                perceptual_hash=response.perceptual_hash,
                eyes_closed_count=response.eyes_closed_count,
                dominant_color=response.dominant_color,
                tags=list(response.tags),
                model_version=response.model_version,
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
