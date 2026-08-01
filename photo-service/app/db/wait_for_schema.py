"""Wait for the Alembic schema to exist before the api/worker containers start.

TASK-003 C1 (tasks/TASK-003/20_design.md §9.6): `kubectl apply -f k8s/` gives
no ordering guarantee between manifests, so `31-api.yaml`/`32-worker.yaml`
run an initContainer on this module (same `photo-service:local` image,
`python -m app.db.wait_for_schema`) to block until the standalone
`30-migrate-job.yaml` Job has run `alembic upgrade head` and created the
`alembic_version` table. This is cheaper than shipping `psql`/a generic
wait-for-it tool in the runtime image, and needs nothing beyond the
dependencies the app already has (SQLAlchemy/asyncpg).

Polling `SELECT 1 FROM alembic_version` checks that the table *exists* -
it does not compare against a specific revision. The migrate Job is the
only writer of that table and it always runs `alembic upgrade head`, so
"the table exists" and "the schema is at head" happen at the same instant
in this deployment; there is deliberately no separate initContainer per
migration step (design §9.6, §9.9 - one Job, not N).

Exit codes (initContainer semantics): 0 = schema ready, main container
starts; 1 = timed out, Kubernetes retries the Pod per its restart policy.
"""

import asyncio
import logging
import sys
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.logging import setup_logging

logger = logging.getLogger(__name__)

# Fixed poll interval - simple and predictable for a short-lived
# initContainer; no exponential backoff needed since there is a single
# hard `timeout_seconds` ceiling regardless of how many attempts it takes.
POLL_INTERVAL_SECONDS = 2.0


async def wait_for_schema(
    database_url: str,
    timeout_seconds: float,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
) -> bool:
    """Poll until `alembic_version` is queryable or `timeout_seconds` elapses.

    Returns True as soon as the query succeeds, False once the deadline has
    passed without success. A dedicated engine is created for the lifetime
    of this call and always disposed before returning - this is a
    short-lived CLI invocation, not a long-running connection pool shared
    with the rest of the app.
    """
    engine = create_async_engine(database_url, pool_pre_ping=True)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1 FROM alembic_version"))
                return True
            except Exception as exc:  # noqa: BLE001 - retry any DB error until the deadline
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "timed out waiting for alembic_version to exist",
                        extra={"error": str(exc), "timeout_seconds": timeout_seconds},
                    )
                    return False
                logger.info(
                    "alembic_version not ready yet, retrying",
                    extra={"remaining_seconds": round(remaining, 1)},
                )
                await asyncio.sleep(min(poll_interval_seconds, remaining))
    finally:
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    ready = asyncio.run(
        wait_for_schema(settings.DATABASE_URL, settings.WAIT_FOR_SCHEMA_TIMEOUT_SECONDS)
    )
    if not ready:
        sys.exit(1)

    logger.info("schema ready, handing off to main container command")


if __name__ == "__main__":
    main()
