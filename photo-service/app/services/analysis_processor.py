"""Domain logic for processing a single `photo.analysis.requested` message.

Shared by `app.worker.consumer` (real Kafka messages) and unit tests (no
Kafka connection needed) - the atomic claim -> gRPC retry loop -> terminal
write sequence from tasks/TASK-002/20_design.md §5.3 lives here so it can
be tested independently of `aiokafka`.

Must not know about Kafka (`aiokafka`) or ASGI - callers own the
`AsyncSession` and the message payload; this class only orchestrates
`repositories/` + the gRPC client.
"""

import asyncio
import logging
import time
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.integrations.analyzer_client import (
    AnalyzerGrpcClient,
    RetryDecision,
    classify_grpc_error,
    error_code_from_exception,
    truncate_error_message,
)
from app.integrations.metrics import (
    analyzer_grpc_errors_total,
    photo_analysis_completed_total,
    photo_analysis_duration_seconds,
    photo_analysis_failed_total,
    photo_analysis_started_total,
    worker_messages_processed_total,
)
from app.repositories.analysis_result_repository import AnalysisResultRepository
from app.repositories.photo_repository import PhotoRepository

logger = logging.getLogger(__name__)


class AnalysisProcessor:
    """Claim -> analyze (with bounded retry) -> persist, for one photo."""

    def __init__(
        self,
        photo_repository: PhotoRepository,
        analysis_repository: AnalysisResultRepository,
        analyzer: AnalyzerGrpcClient,
        settings: Settings,
    ) -> None:
        self._photos = photo_repository
        self._analysis = analysis_repository
        self._analyzer = analyzer
        self._max_attempts = settings.WORKER_MAX_ATTEMPTS
        self._backoff_base = settings.RETRY_BACKOFF_BASE_SECONDS
        self._grpc_timeout = settings.ANALYZER_GRPC_TIMEOUT

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
            worker_messages_processed_total.labels(result="done").inc()
            logger.info(
                "analysis completed",
                extra={"photo_id": photo_id, "attempt": attempts},
            )
            return

        reason = "no_retry" if decision == RetryDecision.NO_RETRY else "retries_exhausted"
        await self._photos.mark_failed(session, photo_uuid, last_code, last_message, attempts)
        await session.commit()
        photo_analysis_failed_total.labels(reason=reason).inc()
        worker_messages_processed_total.labels(result="failed").inc()
        logger.error(
            "analysis failed",
            extra={
                "photo_id": photo_id,
                "attempt": attempts,
                "reason": reason,
                "error_code": last_code,
            },
        )
