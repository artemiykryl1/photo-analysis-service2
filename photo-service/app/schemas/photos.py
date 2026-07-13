"""Pydantic DTOs for the photos domain and shared error response shape.

Upload-related request/response DTOs (e.g. `PhotoUploadResponse`) are
intentionally NOT defined yet - they belong to TASK-001 together with the
`POST /api/v1/photos` endpoint. Only the pieces needed by the bootstrap
skeleton (health check + error format) live here for now.
"""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Body of `GET /healthz`."""

    status: str
    service: str


class ErrorResponse(BaseModel):
    """Unified error body for all `AppError` exceptions (spec §2)."""

    error_code: str
    message: str
    trace_id: str


# TODO(TASK-001): add PhotoUploadResponse (photo_id, user_id, status, created_at)
# and PhotoStatusResponse once POST /api/v1/photos is implemented.
