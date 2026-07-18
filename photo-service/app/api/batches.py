"""HTTP router for the batches domain.

Same rule as `app/api/photos.py`: this module must never import SQLAlchemy
models, `select`, or `session.execute`, and must never talk to MinIO
directly - it only calls into `services/`.

tasks/TASK-002/20_design.md §12 step 24.
"""

import uuid

from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.photos import get_photo_service
from app.db.session import get_session
from app.repositories.batch_repository import BatchRepository
from app.schemas.photos import BatchAcceptedResponse, BatchResponse
from app.services.batch_service import BatchService
from app.services.photo_service import PhotoService

router = APIRouter(prefix="/v1", tags=["batches"])


def get_batch_service(request: Request) -> BatchService:  # noqa: ARG001 - Depends signature
    return BatchService(batch_repository=BatchRepository())


@router.post("/photos/batch", status_code=202, response_model=BatchAcceptedResponse)
async def upload_batch(
    file: list[UploadFile] = File(...),
    service: PhotoService = Depends(get_photo_service),
    session: AsyncSession = Depends(get_session),
) -> BatchAcceptedResponse:
    files = [(f.filename, await f.read()) for f in file]
    return await service.create_batch(session, files)


@router.get("/batches/{batch_id}", response_model=BatchResponse)
async def get_batch(
    batch_id: uuid.UUID,
    service: BatchService = Depends(get_batch_service),
    session: AsyncSession = Depends(get_session),
) -> BatchResponse:
    return await service.get_batch(session, batch_id)
