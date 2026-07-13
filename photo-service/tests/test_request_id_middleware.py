"""Tests for `app.api.middleware.RequestIdMiddleware`.

`/healthz` is used for the plain propagation checks because it has no
DB/MinIO dependency (safe to call against `app.main.app` directly with no
overrides). Propagation into the unified error body is tested against a
small throwaway app (same pattern as tests/test_errors.py) so it does not
depend on a live database either.
"""

import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.middleware import RequestIdMiddleware
from app.core.errors import NotFoundError, register_exception_handlers
from app.main import app as main_app


class TestNonHttpScopePassthrough:
    async def test_lifespan_scope_bypasses_request_id_logic_untouched(self):
        """Non-`http` ASGI scopes (e.g. `lifespan`) must be forwarded to the
        wrapped app as-is - `RequestIdMiddleware` must not touch
        `trace_id_var` or wrap `send` for them."""
        received_scopes = []

        async def inner_app(scope, receive, send):
            received_scopes.append(scope["type"])

        middleware = RequestIdMiddleware(inner_app)

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            pass

        await middleware({"type": "lifespan"}, receive, send)

        assert received_scopes == ["lifespan"]


class TestRequestIdEchoOnHealthz:
    async def test_response_carries_a_generated_x_request_id_when_absent(self):
        transport = ASGITransport(app=main_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")

        assert "x-request-id" in response.headers
        uuid.UUID(response.headers["x-request-id"])  # generated value must be a valid uuid4

    async def test_incoming_x_request_id_header_is_echoed_back_unchanged(self):
        transport = ASGITransport(app=main_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz", headers={"X-Request-ID": "custom-id-123"})

        assert response.headers["x-request-id"] == "custom-id-123"

    async def test_two_requests_without_header_get_different_generated_ids(self):
        transport = ASGITransport(app=main_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/healthz")
            second = await client.get("/healthz")

        assert first.headers["x-request-id"] != second.headers["x-request-id"]


def _make_app_with_middleware_raising(exc: Exception) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise exc

    return app


class TestRequestIdPropagatesIntoErrorBody:
    async def test_incoming_request_id_flows_into_error_response_body(self):
        app = _make_app_with_middleware_raising(NotFoundError("Photo not found"))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/boom", headers={"X-Request-ID": "trace-xyz"})

        assert response.json()["request_id"] == "trace-xyz"
        assert response.headers["x-request-id"] == "trace-xyz"

    async def test_generated_request_id_flows_into_error_response_body_when_absent(self):
        app = _make_app_with_middleware_raising(NotFoundError("Photo not found"))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/boom")

        header_id = response.headers["x-request-id"]
        assert response.json()["request_id"] == header_id
        uuid.UUID(header_id)
