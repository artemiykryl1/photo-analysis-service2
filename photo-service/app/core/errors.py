"""Domain exceptions and their mapping to HTTP responses.

Every domain error is a subclass of `AppError`. `register_exception_handlers`
wires a single FastAPI exception handler that converts any `AppError` into
the unified JSON error body described in feature-upload/spec.md §2:

    { "error_code": "...", "message": "...", "request_id": "..." }
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

    def __init__(self, message: str | None = None, *, photo_id: str | None = None) -> None:
        if message is not None:
            self.message = message
        self.photo_id = photo_id
        super().__init__(self.message)


class StorageUnavailable(AppError):
    """Raised when MinIO is unreachable."""

    error_code = "SERVICE_UNAVAILABLE"
    http_status = 503
    message = "Storage is unreachable"


class DatabaseUnavailable(AppError):
    """Raised when `session.commit()` fails after the file(s) were already
    saved to MinIO (tasks/TASK-002.1/20_design.md F5).

    Deliberately a separate class from `StorageUnavailable` - the failure
    here is the DATABASE commit, not MinIO, and conflating the two would
    mislead an operator reading the logs (and tag the error with the
    wrong root cause). It intentionally reuses the SAME public
    `error_code`/`http_status` as `StorageUnavailable` ("SERVICE_UNAVAILABLE",
    503) so the public error contract (`{error_code, message, request_id}`
    and the set of publicly documented codes) does not change.
    """

    error_code = "SERVICE_UNAVAILABLE"
    http_status = 503
    message = "Service temporarily unavailable"


class NotFoundError(AppError):
    """Raised when a requested resource does not exist."""

    error_code = "NOT_FOUND"
    http_status = 404
    message = "Resource not found"


class ValidationError(AppError):
    """Raised on invalid input (e.g. empty file)."""

    error_code = "INVALID_FILE"
    http_status = 400
    message = "Invalid input"


class ConflictError(AppError):
    """Raised on a uniqueness/duplication conflict."""

    error_code = "CONFLICT"
    http_status = 409
    message = "Conflict"


class PayloadTooLargeError(AppError):
    """Raised when the uploaded file exceeds the size limit (50 MB)."""

    error_code = "PAYLOAD_TOO_LARGE"
    http_status = 413
    message = "File exceeds the maximum allowed size"


class UnsupportedMediaTypeError(AppError):
    """Raised when the uploaded file is not a recognized JPEG/PNG (magic bytes)."""

    error_code = "UNSUPPORTED_MEDIA_TYPE"
    http_status = 415
    message = "Unsupported media type"


class BatchSizeError(AppError):
    """Raised when `POST /v1/photos/batch` receives fewer than 2 or more
    than 10 files (tasks/TASK-002/20_design.md §3.4, §12 step 19)."""

    error_code = "INVALID_BATCH_SIZE"
    http_status = 400
    message = "Batch must contain between 2 and 10 files"


def register_exception_handlers(app: FastAPI) -> None:
    """Register handlers that convert any exception into ErrorResponse JSON.

    Two handlers are registered:
    - `AppError` -> its own `error_code`/`http_status`/`message`.
    - `Exception` (catch-all) -> generic 500 `INTERNAL_ERROR`, so an
      unexpected failure (e.g. `session.commit()` blowing up) still
      returns the unified error body instead of a bare framework 500
      (reviewer-1 N1). The real exception is logged with `exc_info` but
      never leaked into the response body.
    """

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = trace_id_var.get()
        logger.error(
            "request failed with %s: %s",
            exc.error_code,
            exc.message,
            extra={"error_code": exc.error_code, "photo_id": getattr(exc, "photo_id", None)},
        )
        body = ErrorResponse(
            error_code=exc.error_code, message=exc.message, request_id=request_id
        )
        return JSONResponse(status_code=exc.http_status, content=body.model_dump())

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = trace_id_var.get()
        logger.exception(
            "request failed with unhandled exception",
            extra={"error_code": "INTERNAL_ERROR", "photo_id": None},
        )
        body = ErrorResponse(
            error_code="INTERNAL_ERROR",
            message="Internal server error",
            request_id=request_id,
        )
        return JSONResponse(status_code=500, content=body.model_dump())
