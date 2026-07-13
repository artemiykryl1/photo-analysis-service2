"""Structured JSON logging setup.

Logs are emitted as JSON to stdout so they can be collected by the
container runtime / log shipper (ELK, Loki) per constitution.md §3.1.

`trace_id_var` is a request-scoped contextvar. It is populated by
`app.api.middleware.RequestIdMiddleware` (pure-ASGI, TASK-001) from an
inbound `X-Request-ID` header or a freshly generated uuid4, and reset
after the request completes. The same value is exposed in log records
under two names: `trace_id` (constitution.md §3.1 vocabulary) and
`request_id` (feature-upload DoD vocabulary) - both point at the same
underlying contextvar.

Security note (constitution.md §3.3): do not log full MinIO object
paths, raw user_id, or file contents. Keep this in mind when adding
log statements in services/integrations layers.
"""

import logging
import sys
from contextvars import ContextVar

from pythonjsonlogger import jsonlogger

SERVICE_NAME = "photo-service"

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")


class TraceIdFilter(logging.Filter):
    """Injects the current contextvar trace_id/request_id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        current = trace_id_var.get()
        record.trace_id = current
        record.request_id = current
        record.service = SERVICE_NAME
        return True


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with a single JSON stdout handler.

    Idempotent-ish: clears any previously attached handlers on the root
    logger before adding the new one, so calling it twice (e.g. in tests)
    does not duplicate log lines.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level.upper())

    for existing_handler in list(root_logger.handlers):
        root_logger.removeHandler(existing_handler)

    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        fmt=(
            "%(asctime)s %(levelname)s %(name)s %(service)s "
            "%(trace_id)s %(request_id)s %(message)s"
        ),
        rename_fields={"asctime": "timestamp", "levelname": "level"},
    )
    handler.setFormatter(formatter)
    handler.addFilter(TraceIdFilter())
    root_logger.addHandler(handler)

