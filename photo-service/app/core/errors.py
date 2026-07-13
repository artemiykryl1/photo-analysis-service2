"""Domain exceptions and their mapping to HTTP responses.

Every domain error is a subclass of `AppError`. `register_exception_handlers`
wires a single FastAPI exception handler that converts any `AppError` into
the unified JSON error body described in feature-upload/spec.md §2:

    { "error_code": "...", "message": "...", "trace_id": "..." }
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.logging import trace_id_var
from app.schemas.photos import ErrorResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for all domain errors."""

    error_code: str = "INTERNAL_ERROR"
    http_status: int = 500
    message: str = "Internal server error"

    def __init__(self, message: str | None = None) -> None:
        if message is not None:
            self.message = message
        super().__init__(self.message)


class StorageUnavailable(AppError):
    """Raised when MinIO is unreachable."""

    error_code = "SERVICE_UNAVAILABLE"
    http_status = 503
    message = "Storage is unreachable"


class NotFoundError(AppError):
    """Raised when a requested resource does not exist."""

    error_code = "NOT_FOUND"
    http_status = 404
    message = "Resource not found"


class ValidationError(AppError):
    """Raised on invalid input (e.g. bad file type/size)."""

    error_code = "INVALID_FILE"
    http_status = 400
    message = "Invalid input"


class ConflictError(AppError):
    """Raised on a uniqueness/duplication conflict."""

    error_code = "CONFLICT"
    http_status = 409
    message = "Conflict"


def register_exception_handlers(app: FastAPI) -> None:
    """Register a handler that converts any AppError into ErrorResponse JSON."""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        trace_id = trace_id_var.get()
        logger.error(
            "request failed with %s: %s",
            exc.error_code,
            exc.message,
            extra={"error_code": exc.error_code},
        )
        body = ErrorResponse(
            error_code=exc.error_code, message=exc.message, trace_id=trace_id
        )
        return JSONResponse(status_code=exc.http_status, content=body.model_dump())
