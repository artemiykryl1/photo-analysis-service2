"""Background outbox publisher (tasks/TASK-002/20_design.md §4.2).

The HTTP upload path never talks to Kafka directly - it only marks a photo
row `publish_status='not_sent'` in the same transaction as the insert
(see `PhotoService.create_photo`). This coroutine, started as an
`asyncio.Task` from `app.main.lifespan`, is the ONLY code path that calls
`KafkaEventProducer.publish_analysis_requested`. That gives an
at-least-once delivery guarantee (upload never blocks on/fails because of
Kafka) without introducing a real message broker into the HTTP request
path.
"""

import asyncio
import logging
import uuid

from aiokafka.errors import KafkaError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.logging import trace_id_var
from app.integrations.kafka_producer import KafkaEventProducer
from app.integrations.metrics_api import kafka_publish_errors_total
from app.repositories.photo_repository import PhotoRepository

logger = logging.getLogger(__name__)


async def run_outbox_publisher(
    session_factory: async_sessionmaker,
    producer: KafkaEventProducer,
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    """Poll `photos` for `publish_status='not_sent'` rows and publish them.

    Runs until `stop_event` is set (graceful shutdown - see
    `app.main.lifespan`). A publish failure (Kafka down/unreachable) is
    logged and the row is simply left `not_sent` for the next poll - it is
    NOT re-raised, so an unavailable Kafka never crashes this task or the
    application.

    Review-1 fix (tasks/TASK-002/40_review-1.md B1): `producer.ensure_started()`
    is awaited at the top of EVERY iteration, not just once at API startup.
    If `producer.start()` failed in `app.main.lifespan` (Kafka not reachable
    yet at API boot - a real race given KRaft's ~10-20s startup), this is
    what makes the producer self-heal once Kafka becomes reachable, instead
    of every publish attempt failing forever.
    """
    repository = PhotoRepository()

    while not stop_event.is_set():
        try:
            await producer.ensure_started()
        except Exception as exc:  # noqa: BLE001 - Kafka not reachable yet, retry next poll
            logger.warning(
                "Kafka producer not started, will retry on next outbox poll",
                exc_info=exc,
            )
        else:
            try:
                async with session_factory() as session:
                    rows = await repository.fetch_unpublished(session, settings.OUTBOX_BATCH_SIZE)
                    for row in rows:
                        token = trace_id_var.set(row.trace_id or str(uuid.uuid4()))
                        try:
                            await producer.publish_analysis_requested(
                                str(row.photo_id),
                                row.object_key,
                                row.created_at,
                                trace_id_var.get(),
                            )
                            await repository.mark_published(session, row.photo_id)
                        except (KafkaError, TimeoutError) as exc:
                            kafka_publish_errors_total.inc()
                            logger.warning(
                                "failed to publish photo.analysis.requested, "
                                "will retry on next outbox poll",
                                extra={"photo_id": str(row.photo_id)},
                                exc_info=exc,
                            )
                            # Row stays `not_sent`; keep trying the rest of the batch -
                            # a single flaky send should not stall unrelated photos.
                        finally:
                            trace_id_var.reset(token)
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the poll loop itself must never die
                logger.exception("outbox publisher iteration failed unexpectedly")

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.OUTBOX_POLL_INTERVAL_SECONDS
            )
        except TimeoutError:
            pass  # normal case: no shutdown signal within the poll interval
