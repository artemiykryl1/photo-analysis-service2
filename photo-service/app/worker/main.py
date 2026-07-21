"""Worker entrypoint: `python -m app.worker.main`.

Consumes `photo.analysis.requested`, calls the analyzer via gRPC, and
persists results/failures directly to PostgreSQL - no separate
`analysis-results` Kafka topic (tasks/TASK-002/20_design.md §5). Runs as
its own container from the same Docker image as the API, with a
different `command` (see docker-compose.yml `worker` service).

Graceful shutdown (design §5.6, constitution.md §3.2): SIGTERM/SIGINT ->
stop_event -> `consume_loop` finishes its current in-flight message and
returns -> `consumer.stop()` (commits the last offset) -> `analyzer.close()`
(closes the gRPC channel) -> `engine.dispose()`.
"""

import asyncio
import logging
import signal

from aiokafka import AIOKafkaConsumer
from prometheus_client import start_http_server

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import engine
from app.integrations.analyzer_client import AnalyzerGrpcClient
from app.repositories.analysis_result_repository import AnalysisResultRepository
from app.repositories.batch_repository import BatchRepository
from app.repositories.photo_repository import PhotoRepository
from app.services.analysis_processor import AnalysisProcessor
from app.worker.consumer import consume_loop

logger = logging.getLogger(__name__)


def _install_shutdown_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _request_shutdown(signal_name: str) -> None:
        logger.info("shutdown signal received", extra={"signal": signal_name})
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except NotImplementedError:
            # `add_signal_handler` is POSIX-only (raises on Windows event
            # loops) - fall back to the classic `signal.signal` API for
            # local Windows development.
            signal.signal(sig, lambda *_args, _s=sig: _request_shutdown(_s.name))


async def _run() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    start_http_server(settings.WORKER_METRICS_PORT)
    logger.info("worker metrics server started", extra={"port": settings.WORKER_METRICS_PORT})

    analyzer = AnalyzerGrpcClient(settings.ANALYZER_GRPC_ADDR, settings.ANALYZER_GRPC_TIMEOUT)
    processor = AnalysisProcessor(
        photo_repository=PhotoRepository(),
        analysis_repository=AnalysisResultRepository(),
        analyzer=analyzer,
        settings=settings,
        batch_repository=BatchRepository(),
    )

    consumer = AIOKafkaConsumer(
        settings.KAFKA_TOPIC_ANALYSIS_REQUESTED,
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        group_id=settings.KAFKA_CONSUMER_GROUP,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    await consumer.start()
    logger.info(
        "worker consumer started",
        extra={
            "topic": settings.KAFKA_TOPIC_ANALYSIS_REQUESTED,
            "group": settings.KAFKA_CONSUMER_GROUP,
        },
    )

    stop_event = asyncio.Event()
    _install_shutdown_handlers(stop_event)

    try:
        await consume_loop(consumer, processor, stop_event)
    finally:
        logger.info("worker shutting down")
        await consumer.stop()
        await analyzer.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
