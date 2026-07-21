"""Functional (HTTP contract) tests for the 4 `/v1/photos*` endpoints.

Exercises the full ASGI stack (`app.main.app`, including
`RequestIdMiddleware` and `register_exception_handlers`) via
`httpx.ASGITransport`, with `PhotoService`/`AsyncSession` replaced through
`app.dependency_overrides` (same pattern already established in
tests/test_photos_content_disposition.py) - no real Postgres/MinIO
connection is made. Real end-to-end validation/order-of-operations logic
lives in tests/test_photo_service.py; this file verifies HTTP wiring:
request parsing, status codes, response bodies, and the unified error
format for every documented error code.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.photos import get_photo_service
from app.core.errors import (
    ConflictError,
    DatabaseUnavailable,
    NotFoundError,
    PayloadTooLargeError,
    StorageUnavailable,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.db.session import get_session
from app.main import app
from app.schemas.photos import PhotoResponse, UploadPhotoResponse

JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 20
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


@pytest.fixture
async def app_client():
    fake_service = AsyncMock()
    app.dependency_overrides[get_photo_service] = lambda: fake_service
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, fake_service
    finally:
        app.dependency_overrides.clear()


def _unified_error_body(body: dict) -> bool:
    return set(body.keys()) == {"error_code", "message", "request_id"}


class TestUploadPhotoHappyPath:
    async def test_jpeg_upload_returns_202_with_expected_body(self, app_client):
        client, fake_service = app_client
        photo_id = str(uuid.uuid4())
        fake_service.create_photo.return_value = UploadPhotoResponse(
            photo_id=photo_id, status="pending"
        )

        response = await client.post(
            "/v1/photos", files={"file": ("cat.jpg", JPEG_BYTES, "image/jpeg")}
        )

        assert response.status_code == 202
        assert response.json() == {"photo_id": photo_id, "status": "pending"}

    async def test_png_upload_returns_202(self, app_client):
        client, fake_service = app_client
        photo_id = str(uuid.uuid4())
        fake_service.create_photo.return_value = UploadPhotoResponse(
            photo_id=photo_id, status="pending"
        )

        response = await client.post(
            "/v1/photos", files={"file": ("cat.png", PNG_BYTES, "image/png")}
        )

        assert response.status_code == 202
        assert response.json()["status"] == "pending"

    async def test_service_receives_filename_and_raw_bytes(self, app_client):
        client, fake_service = app_client
        fake_service.create_photo.return_value = UploadPhotoResponse(
            photo_id=str(uuid.uuid4()), status="pending"
        )

        await client.post("/v1/photos", files={"file": ("cat.jpg", JPEG_BYTES, "image/jpeg")})

        fake_service.create_photo.assert_awaited_once()
        call = fake_service.create_photo.call_args
        assert call.args[1] == "cat.jpg"
        assert call.args[2] == JPEG_BYTES


class TestUploadPhotoErrorMapping:
    """Every documented upload error code (design §2.1/§6) must produce the
    unified error body with the right HTTP status and error_code."""

    @pytest.mark.parametrize(
        "exc, expected_status, expected_code",
        [
            (ValidationError("Uploaded file is empty"), 400, "INVALID_FILE"),
            (PayloadTooLargeError(), 413, "PAYLOAD_TOO_LARGE"),
            (UnsupportedMediaTypeError(), 415, "UNSUPPORTED_MEDIA_TYPE"),
            (ConflictError(), 409, "CONFLICT"),
            (StorageUnavailable(), 503, "SERVICE_UNAVAILABLE"),
            # TASK-002.1 F5: a commit failure after the file is already in
            # MinIO surfaces as DatabaseUnavailable - same public
            # error_code/status as StorageUnavailable (contract unchanged).
            (DatabaseUnavailable(), 503, "SERVICE_UNAVAILABLE"),
        ],
    )
    async def test_upload_error_returns_expected_status_and_unified_body(
        self, app_client, exc, expected_status, expected_code
    ):
        client, fake_service = app_client
        fake_service.create_photo.side_effect = exc

        response = await client.post(
            "/v1/photos", files={"file": ("x.jpg", JPEG_BYTES, "image/jpeg")}
        )

        assert response.status_code == expected_status
        body = response.json()
        assert _unified_error_body(body)
        assert body["error_code"] == expected_code

    async def test_empty_file_body_still_reaches_service_as_empty_bytes(self, app_client):
        """The endpoint must not silently reject an empty file itself - real
        validation (empty -> 400) is the service's job (see
        test_photo_service.py); here we only check the plumbing forwards an
        empty payload through untouched."""
        client, fake_service = app_client
        fake_service.create_photo.side_effect = ValidationError("Uploaded file is empty")

        response = await client.post("/v1/photos", files={"file": ("empty.jpg", b"", "image/jpeg")})

        assert response.status_code == 400
        call = fake_service.create_photo.call_args
        assert call.args[2] == b""


class TestListPhotos:
    async def test_empty_list_returns_200_empty_array(self, app_client):
        client, fake_service = app_client
        fake_service.list_photos.return_value = []

        response = await client.get("/v1/photos")

        assert response.status_code == 200
        assert response.json() == []

    async def test_list_with_items_returns_photo_response_shape(self, app_client):
        client, fake_service = app_client
        item = PhotoResponse(id=str(uuid.uuid4()), filename="a.jpg", status="pending")
        fake_service.list_photos.return_value = [item]

        response = await client.get("/v1/photos")

        assert response.status_code == 200
        assert response.json() == [item.model_dump()]

    async def test_default_limit_and_offset_are_50_and_0(self, app_client):
        client, fake_service = app_client
        fake_service.list_photos.return_value = []

        await client.get("/v1/photos")

        call = fake_service.list_photos.call_args
        assert call.args[1] == 50
        assert call.args[2] == 0

    async def test_custom_limit_and_offset_are_forwarded(self, app_client):
        client, fake_service = app_client
        fake_service.list_photos.return_value = []

        await client.get("/v1/photos?limit=10&offset=5")

        call = fake_service.list_photos.call_args
        assert call.args[1] == 10
        assert call.args[2] == 5

    @pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
    async def test_out_of_range_pagination_params_return_422(self, app_client, query):
        client, _fake_service = app_client

        response = await client.get(f"/v1/photos?{query}")

        assert response.status_code == 422


class TestGetPhotoStatus:
    async def test_existing_photo_returns_200_with_photo_response(self, app_client):
        client, fake_service = app_client
        photo_id = uuid.uuid4()
        fake_service.get_photo.return_value = PhotoResponse(
            id=str(photo_id), filename="a.jpg", status="pending"
        )

        response = await client.get(f"/v1/photos/{photo_id}")

        assert response.status_code == 200
        assert response.json() == {
            "id": str(photo_id),
            "filename": "a.jpg",
            "status": "pending",
            "analysis": None,
        }

    async def test_missing_photo_returns_404_unified_body(self, app_client):
        client, fake_service = app_client
        fake_service.get_photo.side_effect = NotFoundError("Photo not found")

        response = await client.get(f"/v1/photos/{uuid.uuid4()}")

        assert response.status_code == 404
        body = response.json()
        assert _unified_error_body(body)
        assert body["error_code"] == "NOT_FOUND"

    async def test_non_uuid_path_param_returns_422_not_500(self, app_client):
        client, _fake_service = app_client

        response = await client.get("/v1/photos/not-a-uuid")

        assert response.status_code == 422


class TestGetPhotoContent:
    async def test_existing_photo_returns_200_with_bytes_and_content_type(self, app_client):
        client, fake_service = app_client
        fake_service.get_photo_content.return_value = (JPEG_BYTES, "image/jpeg", "cat.jpg")

        response = await client.get(f"/v1/photos/{uuid.uuid4()}/content")

        assert response.status_code == 200
        assert response.content == JPEG_BYTES
        assert response.headers["content-type"].startswith("image/jpeg")
        assert 'filename="cat.jpg"' in response.headers["content-disposition"]

    async def test_png_content_type_is_image_png(self, app_client):
        client, fake_service = app_client
        fake_service.get_photo_content.return_value = (PNG_BYTES, "image/png", "cat.png")

        response = await client.get(f"/v1/photos/{uuid.uuid4()}/content")

        assert response.headers["content-type"].startswith("image/png")

    async def test_missing_record_returns_404(self, app_client):
        client, fake_service = app_client
        fake_service.get_photo_content.side_effect = NotFoundError("Photo not found")

        response = await client.get(f"/v1/photos/{uuid.uuid4()}/content")

        assert response.status_code == 404
        assert _unified_error_body(response.json())

    async def test_missing_object_in_storage_returns_404(self, app_client):
        """`NotFoundError` is raised both for "no DB row" and "no MinIO
        object" (see design §2.4) - either way the endpoint must answer 404."""
        client, fake_service = app_client
        fake_service.get_photo_content.side_effect = NotFoundError("Object not found")

        response = await client.get(f"/v1/photos/{uuid.uuid4()}/content")

        assert response.status_code == 404

    async def test_storage_unavailable_returns_503(self, app_client):
        client, fake_service = app_client
        fake_service.get_photo_content.side_effect = StorageUnavailable("MinIO is unreachable")

        response = await client.get(f"/v1/photos/{uuid.uuid4()}/content")

        assert response.status_code == 503
        assert _unified_error_body(response.json())

    async def test_non_uuid_path_param_returns_422(self, app_client):
        client, _fake_service = app_client

        response = await client.get("/v1/photos/not-a-uuid/content")

        assert response.status_code == 422


class TestGetPhotoServiceDependency:
    """`get_photo_service` is the DI wiring used by every route above (via
    `app.dependency_overrides` in the `app_client` fixture); this test
    exercises the real, un-overridden function directly."""

    def test_builds_service_from_request_app_state_storage(self):
        from app.integrations.storage import ObjectStorage
        from app.services.photo_service import PhotoService

        fake_request = MagicMock()
        fake_storage = MagicMock(spec=ObjectStorage)
        fake_request.app.state.storage = fake_storage

        service = get_photo_service(fake_request)

        assert isinstance(service, PhotoService)
        assert service._storage is fake_storage


class TestErrorBodyRequestId:
    async def test_error_response_request_id_echoes_incoming_header(self, app_client):
        client, fake_service = app_client
        fake_service.get_photo.side_effect = NotFoundError("Photo not found")

        response = await client.get(
            f"/v1/photos/{uuid.uuid4()}", headers={"X-Request-ID": "my-trace-id"}
        )

        assert response.json()["request_id"] == "my-trace-id"
        assert response.headers["x-request-id"] == "my-trace-id"
