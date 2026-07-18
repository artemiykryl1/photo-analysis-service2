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


class AnalysisResultResponse(BaseModel):
    """Analyzer result, embedded in `PhotoResponse.analysis` once the
    photo reaches `status=done` (tasks/TASK-002/20_design.md §3.3)."""

    faces_count: int
    is_blurred: bool
    blur_score: float
    perceptual_hash: str


class PhotoResponse(BaseModel):
    """Body of `GET /v1/photos/{photo_id}` and items of `GET /v1/photos`.

    `analysis` defaults to `None` so the TASK-001 response contract is
    unchanged for callers that don't look at the new field (backward
    compatible addition, TASK-002 design §3.3).
    """

    id: str
    filename: str
    status: Literal["pending", "processing", "done", "failed"]
    analysis: AnalysisResultResponse | None = None


class BatchPhotoItem(BaseModel):
    """One element of `BatchAcceptedResponse.photos` (design §3.3)."""

    photo_id: str
    status: Literal["pending"]


class BatchAcceptedResponse(BaseModel):
    """Body of `POST /v1/photos/batch` on success (HTTP 202)."""

    batch_id: str
    photos: list[BatchPhotoItem]


class BatchPhotoDetail(BaseModel):
    """One element of `BatchResponse.photos` (design §3.3)."""

    photo_id: str
    filename: str
    status: Literal["pending", "processing", "done", "failed"]
    analysis: AnalysisResultResponse | None = None


class BatchResponse(BaseModel):
    """Body of `GET /v1/batches/{batch_id}`.

    `status` is `completed` only once every photo in the batch has
    reached a terminal status (`done`/`failed`); `best_photo_id` is `None`
    while `processing`, and also `None` if `completed` but every photo
    ended up `failed` (design §3.3, formula in `app.services.batch_service`).
    """

    batch_id: str
    status: Literal["processing", "completed"]
    photos: list[BatchPhotoDetail]
    best_photo_id: str | None = None
