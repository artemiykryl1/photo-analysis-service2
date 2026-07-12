"""HTTP router for the photos domain.

Rule for reviewers: this module must never import SQLAlchemy models,
`select`, or `session.execute`, and must never import `app.integrations.storage`
for direct MinIO calls - it may only call into `services/`.

`healthz`/`readyz` are infrastructure endpoints, not part of the photos
domain, and live in `main.py`.
"""

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.integrations.storage import ObjectStorage
from app.repositories.photo_repository import PhotoRepository
from app.schemas.photos import PhotoResponse, UploadPhotoResponse
from app.services.photo_service import PhotoService

router = APIRouter(prefix="/v1/photos", tags=["photos"])


def get_photo_service(request: Request) -> PhotoService:
    storage: ObjectStorage = request.app.state.storage
    return PhotoService(repository=PhotoRepository(), storage=storage)


def _content_disposition(filename: str) -> str:
    """Build a `Content-Disposition` header value safe for arbitrary
    (including non-ASCII, e.g. Cyrillic) filenames.

    HTTP header values are encoded as latin-1 by Starlette/ASGI, so a raw
    UTF-8 filename (e.g. `фото.jpg`) would raise `UnicodeEncodeError` and turn a
    valid request into a 500. Fixed per RFC 6266/5987: an ASCII-only
    `filename="..."` fallback (quotes/backslashes stripped so it cannot
    break the header syntax) plus a `filename*=UTF-8''<percent-encoded>`
    extended parameter carrying the exact original name for clients that
    support it (reviewer-1 B1).
    """
    ascii_name = filename.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.replace("\\", "").replace('"', "")
    if not ascii_name:
        ascii_name = "download"
    encoded_name = quote(filename, safe="")
    return f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'


@router.post("", status_code=202, response_model=UploadPhotoResponse)
async def upload_photo(
    file: UploadFile = File(...),
    service: PhotoService = Depends(get_photo_service),
    session: AsyncSession = Depends(get_session),
) -> UploadPhotoResponse:
    data = await file.read()
    return await service.create_photo(session, file.filename, data)


@router.get("", response_model=list[PhotoResponse])
async def list_photos(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: PhotoService = Depends(get_photo_service),
    session: AsyncSession = Depends(get_session),
) -> list[PhotoResponse]:
    return await service.list_photos(session, limit, offset)


@router.get("/{photo_id}", response_model=PhotoResponse)
async def get_photo_status(
    photo_id: uuid.UUID,
    service: PhotoService = Depends(get_photo_service),
    session: AsyncSession = Depends(get_session),
) -> PhotoResponse:
    return await service.get_photo(session, photo_id)


@router.get("/{photo_id}/content")
async def download_photo_content(
    photo_id: uuid.UUID,
    service: PhotoService = Depends(get_photo_service),
    session: AsyncSession = Depends(get_session),
) -> Response:
    data, content_type, filename = await service.get_photo_content(session, photo_id)
    headers = {"Content-Disposition": _content_disposition(filename)}
    return Response(content=data, media_type=content_type, headers=headers)
