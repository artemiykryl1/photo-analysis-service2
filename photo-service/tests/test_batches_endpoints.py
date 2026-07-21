"""Functional (HTTP contract) tests for `/v1/photos/batch` and
`/v1/batches/{id}`.

Same pattern as tests/test_photos_endpoints.py: exercises the full ASGI
stack via `httpx.ASGITransport`, with `PhotoService`/`BatchService`/
`AsyncSession` replaced through `app.dependency_overrides` - no real
Postgres/MinIO connection is made. Business-rule edge cases (validation
order, atomicity) are covered at the service level in
tests/test_photo_service.py; this file verifies HTTP wiring: multipart
parsing, status codes, response bodies, and the unified error format.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.batches import get_batch_service
from app.api.photos import get_photo_service
from app.core.errors import (
    BatchSizeError,
    DatabaseUnavailable,
    NotFoundError,
    PayloadTooLargeError,
    StorageUnavailable,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.db.session import get_session
from app.main import app
from app.schemas.photos import (
    AnalysisResultResponse,
    BatchAcceptedResponse,
    BatchPhotoDetail,
    BatchPhotoItem,
    BatchResponse,
)

JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 20
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


@pytest.fixture
async def app_client():
    fake_photo_service = AsyncMock()
    fake_batch_service = AsyncMock()
    app.dependency_overrides[get_photo_service] = lambda: fake_photo_service
    app.dependency_overrides[get_batch_service] = lambda: fake_batch_service
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, fake_photo_service, fake_batch_service
    finally:
        app.dependency_overrides.clear()


def _unified_error_body(body: dict) -> bool:
    return set(body.keys()) == {"error_code", "message", "request_id"}


def _files(n: int, magic: bytes = JPEG_BYTES) -> list[tuple[str, tuple]]:
    return [("file", (f"f{i}.jpg", magic, "image/jpeg")) for i in range(n)]


class TestUploadBatchHappyPath:
    async def test_two_files_returns_202_with_batch_body(self, app_client):
        client, fake_photo_service, _ = app_client
        batch_id = str(uuid.uuid4())
        p1, p2 = str(uuid.uuid4()), str(uuid.uuid4())
        fake_photo_service.create_batch.return_value = BatchAcceptedResponse(
            batch_id=batch_id,
            photos=[
                BatchPhotoItem(photo_id=p1, status="pending"),
                BatchPhotoItem(photo_id=p2, status="pending"),
            ],
        )

        response = await client.post("/v1/photos/batch", files=_files(2))

        assert response.status_code == 202
        body = response.json()
        assert body["batch_id"] == batch_id
        assert body["photos"] == [
            {"photo_id": p1, "status": "pending"},
            {"photo_id": p2, "status": "pending"},
        ]

    async def test_ten_files_returns_202(self, app_client):
        client, fake_photo_service, _ = app_client
        fake_photo_service.create_batch.return_value = BatchAcceptedResponse(
            batch_id=str(uuid.uuid4()),
            photos=[BatchPhotoItem(photo_id=str(uuid.uuid4()), status="pending") for _ in range(10)],
        )

        response = await client.post("/v1/photos/batch", files=_files(10))

        assert response.status_code == 202
        assert len(response.json()["photos"]) == 10

    async def test_service_receives_filenames_and_raw_bytes(self, app_client):
        client, fake_photo_service, _ = app_client
        fake_photo_service.create_batch.return_value = BatchAcceptedResponse(
            batch_id=str(uuid.uuid4()), photos=[]
        )

        await client.post(
            "/v1/photos/batch",
            files=[
                ("file", ("a.jpg", JPEG_BYTES, "image/jpeg")),
                ("file", ("b.png", PNG_BYTES, "image/png")),
            ],
        )

        fake_photo_service.create_batch.assert_awaited_once()
        call = fake_photo_service.create_batch.call_args
        forwarded_files = call.args[1]
        assert forwarded_files == [("a.jpg", JPEG_BYTES), ("b.png", PNG_BYTES)]


class TestUploadBatchErrorMapping:
    """Every documented batch error code (design §3.4) must produce the
    unified error body with the right HTTP status and error_code - the
    service layer raises them, the router only needs to let them flow
    through the existing exception handlers."""

    @pytest.mark.parametrize(
        "exc, expected_status, expected_code",
        [
            (BatchSizeError(), 400, "INVALID_BATCH_SIZE"),
            (ValidationError("empty file in batch"), 400, "INVALID_FILE"),
            (PayloadTooLargeError(), 413, "PAYLOAD_TOO_LARGE"),
            (UnsupportedMediaTypeError(), 415, "UNSUPPORTED_MEDIA_TYPE"),
            (StorageUnavailable(), 503, "SERVICE_UNAVAILABLE"),
            # TASK-002.1 F5: a commit failure after every file is already in
            # MinIO surfaces as DatabaseUnavailable - same public
            # error_code/status as StorageUnavailable (contract unchanged).
            (DatabaseUnavailable(), 503, "SERVICE_UNAVAILABLE"),
        ],
    )
    async def test_error_returns_expected_status_and_unified_body(
        self, app_client, exc, expected_status, expected_code
    ):
        client, fake_photo_service, _ = app_client
        fake_photo_service.create_batch.side_effect = exc

        response = await client.post("/v1/photos/batch", files=_files(2))

        assert response.status_code == expected_status
        body = response.json()
        assert _unified_error_body(body)
        assert body["error_code"] == expected_code

    async def test_single_file_batch_is_rejected_by_service_with_400(self, app_client):
        """The router forwards whatever the client sent - `< 2` is a
        service-level rule (tested directly in test_photo_service.py); here
        we only check the error propagates as 400 INVALID_BATCH_SIZE."""
        client, fake_photo_service, _ = app_client
        fake_photo_service.create_batch.side_effect = BatchSizeError(
            "Batch must contain between 2 and 10 files (got 1)"
        )

        response = await client.post("/v1/photos/batch", files=_files(1))

        assert response.status_code == 400
        assert response.json()["error_code"] == "INVALID_BATCH_SIZE"


class TestGetBatch:
    async def test_existing_batch_processing_returns_200(self, app_client):
        client, _, fake_batch_service = app_client
        batch_id = uuid.uuid4()
        photo_id = str(uuid.uuid4())
        fake_batch_service.get_batch.return_value = BatchResponse(
            batch_id=str(batch_id),
            status="processing",
            photos=[
                BatchPhotoDetail(photo_id=photo_id, filename="a.jpg", status="pending", analysis=None)
            ],
            best_photo_id=None,
        )

        response = await client.get(f"/v1/batches/{batch_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "processing"
        assert body["best_photo_id"] is None
        assert body["photos"][0]["analysis"] is None

    async def test_completed_batch_returns_best_photo_id_and_analysis(self, app_client):
        client, _, fake_batch_service = app_client
        batch_id = uuid.uuid4()
        best_id = str(uuid.uuid4())
        fake_batch_service.get_batch.return_value = BatchResponse(
            batch_id=str(batch_id),
            status="completed",
            photos=[
                BatchPhotoDetail(
                    photo_id=best_id,
                    filename="a.jpg",
                    status="done",
                    analysis=AnalysisResultResponse(
                        faces_count=2, is_blurred=False, blur_score=0.1, perceptual_hash="abc"
                    ),
                )
            ],
            best_photo_id=best_id,
        )

        response = await client.get(f"/v1/batches/{batch_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["best_photo_id"] == best_id
        assert body["photos"][0]["analysis"]["faces_count"] == 2

    async def test_missing_batch_returns_404_unified_body(self, app_client):
        client, _, fake_batch_service = app_client
        fake_batch_service.get_batch.side_effect = NotFoundError("Batch not found")

        response = await client.get(f"/v1/batches/{uuid.uuid4()}")

        assert response.status_code == 404
        body = response.json()
        assert _unified_error_body(body)
        assert body["error_code"] == "NOT_FOUND"

    async def test_non_uuid_path_param_returns_422(self, app_client):
        client, _, _fake_batch_service = app_client

        response = await client.get("/v1/batches/not-a-uuid")

        assert response.status_code == 422


class TestGetBatchServiceDependency:
    def test_builds_batch_service_from_repository(self):
        from app.services.batch_service import BatchService

        fake_request = AsyncMock()
        service = get_batch_service(fake_request)

        assert isinstance(service, BatchService)
