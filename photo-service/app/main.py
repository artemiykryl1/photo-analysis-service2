"""Application entry point: FastAPI app assembly.

Wires together configuration, logging, lifespan (MinIO bucket bootstrap,
DB engine warm-up/dispose), exception handlers and routers. No business
logic lives here.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from sqlalchemy import text

from app.api import photos
from app.api.middleware import RequestIdMiddleware
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.db.session import engine
from app.integrations.storage import ObjectStorage
from app.schemas.photos import HealthResponse

settings = get_settings()
setup_logging(settings.LOG_LEVEL)

logger = logging.getLogger(__name__)


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

    yield

    # Shutdown: release DB connections gracefully.
    await engine.dispose()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

app.add_middleware(RequestIdMiddleware)
register_exception_handlers(app)
app.include_router(photos.router)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness probe: always 200, no external dependency calls."""
    return HealthResponse(status="ok", service=settings.APP_NAME)


@app.get("/readyz")
async def readyz() -> dict:
    """Readiness probe: checks DB and MinIO availability.

    Simplified for bootstrap: failures return 503 but do not crash the
    process (liveness is reported separately via /healthz). Full
    dependency-aware readiness is future work.
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
