"""Pydantic DTOs for the photos domain and shared error response shape."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Body of `GET /healthz`."""

    status: str
    service: str


class ErrorResponse(BaseModel):
    """Unified error body for all `AppError` exceptions (spec §2)."""

    error_code: str
    message: str
    request_id: str


class UploadPhotoResponse(BaseModel):
    """Body of `POST /v1/photos` on success (HTTP 202)."""

    photo_id: str
    status: Literal["pending"]


class PhotoResponse(BaseModel):
    """Body of `GET /v1/photos/{photo_id}` and items of `GET /v1/photos`."""

    id: str
    filename: str
    status: Literal["pending", "processing", "done", "failed"]
