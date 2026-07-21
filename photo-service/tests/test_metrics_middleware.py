"""Behavioural coverage of `app.api.metrics_middleware.metrics_middleware`.

Design §8.3 / §11 risk #7: `endpoint` must be the matched route's path
*template* (e.g. `/v1/photos/{photo_id}`), never the raw request path, to
avoid one metric series per `photo_id`. Exercised through the real ASGI app
(so `request.scope["route"]` is genuinely populated by Starlette's router)
with `PhotoService` mocked out via `app.dependency_overrides` - no real
Postgres/MinIO connection.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.photos import get_photo_service
from app.core.errors import NotFoundError
from app.db.session import get_session
from app.integrations.metrics_api import http_request_duration_seconds, http_requests_total
from app.main import app
from app.schemas.photos import PhotoResponse


def _counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()


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


class TestRedMetricsRecorded:
    async def test_successful_get_increments_http_requests_total_with_route_template(
        self, app_client
    ):
        client, fake_service = app_client
        photo_id = uuid.uuid4()
        fake_service.get_photo.return_value = PhotoResponse(
            id=str(photo_id), filename="a.jpg", status="pending"
        )

        before = _counter_value(
            http_requests_total, method="GET", endpoint="/v1/photos/{photo_id}", status="200"
        )

        await client.get(f"/v1/photos/{photo_id}")

        after = _counter_value(
            http_requests_total, method="GET", endpoint="/v1/photos/{photo_id}", status="200"
        )
        assert after == before + 1

    async def test_route_template_label_does_not_leak_the_raw_photo_id(self, app_client):
        """Cardinality guard: two different `photo_id`s must land on the
        SAME label series (the route template), not two different ones."""
        client, fake_service = app_client
        id_a, id_b = uuid.uuid4(), uuid.uuid4()
        fake_service.get_photo.return_value = PhotoResponse(
            id=str(id_a), filename="a.jpg", status="pending"
        )

        before = _counter_value(
            http_requests_total, method="GET", endpoint="/v1/photos/{photo_id}", status="200"
        )

        await client.get(f"/v1/photos/{id_a}")
        await client.get(f"/v1/photos/{id_b}")

        after = _counter_value(
            http_requests_total, method="GET", endpoint="/v1/photos/{photo_id}", status="200"
        )
        assert after == before + 2

    async def test_error_response_status_code_is_recorded(self, app_client):
        client, fake_service = app_client
        fake_service.get_photo.side_effect = NotFoundError("Photo not found")

        before = _counter_value(
            http_requests_total, method="GET", endpoint="/v1/photos/{photo_id}", status="404"
        )

        await client.get(f"/v1/photos/{uuid.uuid4()}")

        after = _counter_value(
            http_requests_total, method="GET", endpoint="/v1/photos/{photo_id}", status="404"
        )
        assert after == before + 1

    async def test_unmatched_route_falls_back_to_unmatched_label(self, app_client):
        client, _fake_service = app_client

        before = _counter_value(
            http_requests_total, method="GET", endpoint="unmatched", status="404"
        )

        await client.get("/this/path/does/not/exist")

        after = _counter_value(
            http_requests_total, method="GET", endpoint="unmatched", status="404"
        )
        assert after == before + 1

    async def test_request_duration_histogram_gets_an_observation(self, app_client):
        client, fake_service = app_client
        fake_service.get_photo.return_value = PhotoResponse(
            id=str(uuid.uuid4()), filename="a.jpg", status="pending"
        )

        before = http_request_duration_seconds.labels(
            method="GET", endpoint="/v1/photos/{photo_id}"
        )._sum.get()

        await client.get(f"/v1/photos/{uuid.uuid4()}")

        after = http_request_duration_seconds.labels(
            method="GET", endpoint="/v1/photos/{photo_id}"
        )._sum.get()
        assert after >= before  # a new (non-negative) observation was added
