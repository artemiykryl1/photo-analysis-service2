"""Application entry point: FastAPI app assembly.

Wires together configuration, logging, lifespan (MinIO bucket bootstrap,
DB engine warm-up/dispose, TASK-002 Kafka producer + outbox publisher),
exception handlers and routers. No business logic lives here.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import batches, photos
from app.api.metrics_middleware import metrics_middleware
from app.api.middleware import RequestIdMiddleware
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.db.session import SessionLocal, engine, get_session
from app.integrations.kafka_producer import KafkaEventProducer
from app.integrations.metrics import photos_pending
from app.integrations.storage import ObjectStorage
from app.repositories.photo_repository import PhotoRepository
from app.schemas.photos import HealthResponse
from app.services.outbox import run_outbox_publisher

settings = get_settings()
setup_logging(settings.LOG_LEVEL)

logger = logging.getLogger(__name__)

# constitution.md §3.2: 15s budget for graceful shutdown of in-flight work.
OUTBOX_SHUTDOWN_TIMEOUT_SECONDS = 15
# Bound the worst case if the configured Kafka host is unroutable/unresolvable
# at startup - upload/readiness must not be delayed by a slow DNS/connect
# timeout (constitution.md §3.2, design §4.3 "must not fail startup").
PRODUCER_STARTUP_TIMEOUT_SECONDS = 10
# design §8.4: bound the `photos_pending` refresh query on every /metrics
# scrape - if the DB is slow/unavailable, expose stale metrics rather than
# hang the scrape (constitution.md §3.2, DB timeout budget).
METRICS_DB_TIMEOUT_SECONDS = 3


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup: ensure the MinIO bucket exists and warm up the DB connection.
    storage = ObjectStorage(settings)
    try:
        storage.ensure_bucket()
    except Exception:  # noqa: BLE001 - startup must not crash the process
        logger.warning("could not ensure MinIO bucket on startup", exc_info=True)
    app.state.storage = storage

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - startup must not crash the process
        logger.warning("database not reachable on startup", exc_info=True)

    # TASK-002 (tasks/TASK-002/20_design.md §4.3): upload must never depend
    # on Kafka being reachable - a failed producer `start()` is logged and
    # swallowed; the outbox publisher below keeps retrying every poll.
    producer = KafkaEventProducer(settings)
    try:
        async with asyncio.timeout(PRODUCER_STARTUP_TIMEOUT_SECONDS):
            await producer.start()
    except Exception:  # noqa: BLE001 - startup must not crash the process
        logger.warning("could not start Kafka producer on startup", exc_info=True)
    app.state.producer = producer

    stop_event = asyncio.Event()
    app.state.outbox_stop_event = stop_event
    app.state.outbox_task = asyncio.create_task(
        run_outbox_publisher(SessionLocal, producer, settings, stop_event)
    )

    yield

    # Shutdown: stop the outbox publisher, then the Kafka producer, then
    # release DB connections (constitution.md §3.2 graceful shutdown order).
    stop_event.set()
    try:
        await asyncio.wait_for(app.state.outbox_task, timeout=OUTBOX_SHUTDOWN_TIMEOUT_SECONDS)
    except TimeoutError:
        app.state.outbox_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await app.state.outbox_task

    try:
        await producer.stop()
    except Exception:  # noqa: BLE001 - shutdown must not crash the process
        logger.warning("error stopping Kafka producer", exc_info=True)

    await engine.dispose()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

app.add_middleware(RequestIdMiddleware)
app.middleware("http")(metrics_middleware)
register_exception_handlers(app)
app.include_router(photos.router)
app.include_router(batches.router)


@app.get("/metrics")
async def metrics(session: AsyncSession = Depends(get_session)) -> Response:
    """Prometheus text-exposition endpoint (design §8.4).

    `photos_pending` is refreshed from the DB on every scrape (`SELECT
    count(*) WHERE status='pending'`) rather than incremented/decremented
    on every status transition - a query failure/timeout is logged and
    swallowed, leaving the gauge at its last known value instead of
    failing the whole scrape.
    """
    try:
        async with asyncio.timeout(METRICS_DB_TIMEOUT_SECONDS):
            count = await PhotoRepository().count_pending(session)
        photos_pending.set(count)
    except Exception:  # noqa: BLE001 - a metrics scrape must never 500
        logger.warning("failed to refresh photos_pending gauge on scrape", exc_info=True)

    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness probe: always 200, no external dependency calls."""
    return HealthResponse(status="ok", service=settings.APP_NAME)


@app.get("/readyz")
async def readyz() -> dict:
    """Readiness probe: checks DB and MinIO availability.

    Simplified for bootstrap: failures return 503 but do not crash the
    process (liveness is reported separately via /healthz). Full
    dependency-aware readiness (including Kafka) is future work.
    """
    db_ok = True
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False

    storage_ok = True
    try:
        app.state.storage.ensure_bucket()
    except Exception:  # noqa: BLE001
        storage_ok = False

    if db_ok and storage_ok:
        return {"status": "ready"}

    return Response(
        status_code=503,
        media_type="application/json",
        content='{"status":"not_ready"}',
    )
