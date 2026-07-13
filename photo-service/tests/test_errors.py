"""Tests for `app.core.errors`: domain exceptions and their HTTP mapping.

Covers the unified error body contract from feature-upload/spec.md §2:
`{ "error_code": "...", "message": "...", "trace_id": "..." }`, and the
status code mapping table from tasks/TASK-000/20_design.md §3.3
(StorageUnavailable->503, NotFoundError->404, ValidationError->400,
ConflictError->409, base AppError->500).

We register a throwaway route on a fresh FastAPI app (rather than
reusing app.main.app) so each test is isolated from main.py's lifespan
and existing routes, and failures cannot be masked by unrelated app
state.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    StorageUnavailable,
    ValidationError,
    register_exception_handlers,
)
from app.core.logging import trace_id_var


def _make_app_raising(exc: Exception) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise exc

    return app


@pytest.fixture
async def raising_client_factory():
    async def _factory(exc: Exception):
        app = _make_app_raising(exc)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    return _factory


class TestStatusCodeMapping:
    @pytest.mark.parametrize(
        "exc, expected_status, expected_error_code",
        [
            (StorageUnavailable(), 503, "SERVICE_UNAVAILABLE"),
            (NotFoundError(), 404, "NOT_FOUND"),
            (ValidationError(), 400, "INVALID_FILE"),
            (ConflictError(), 409, "CONFLICT"),
            (AppError(), 500, "INTERNAL_ERROR"),
        ],
    )
    async def test_exception_maps_to_expected_status_and_code(
        self, raising_client_factory, exc, expected_status, expected_error_code
    ):
        async with await raising_client_factory(exc) as client:
            response = await client.get("/boom")

        assert response.status_code == expected_status
        body = response.json()
        assert body["error_code"] == expected_error_code


class TestErrorBodyShape:
    async def test_body_has_exactly_error_code_message_trace_id(self, raising_client_factory):
        async with await raising_client_factory(StorageUnavailable("MinIO is unreachable")) as client:
            response = await client.get("/boom")

        body = response.json()
        assert set(body.keys()) == {"error_code", "message", "trace_id"}
        assert body["message"] == "MinIO is unreachable"

    async def test_custom_message_overrides_default(self, raising_client_factory):
        async with await raising_client_factory(ValidationError("File size exceeds 50MB")) as client:
            response = await client.get("/boom")

        assert response.json()["message"] == "File size exceeds 50MB"

    async def test_default_message_used_when_not_provided(self, raising_client_factory):
        async with await raising_client_factory(NotFoundError()) as client:
            response = await client.get("/boom")

        assert response.json()["message"] == "Resource not found"

    async def test_trace_id_reflects_current_contextvar(self, raising_client_factory):
        token = trace_id_var.set("test-trace-123")
        try:
            async with await raising_client_factory(StorageUnavailable()) as client:
                response = await client.get("/boom")
        finally:
            trace_id_var.reset(token)

        assert response.json()["trace_id"] == "test-trace-123"

    async def test_trace_id_defaults_to_placeholder_when_unset(self, raising_client_factory):
        # trace_id_var default is "-" until a middleware populates it (TASK-001).
        async with await raising_client_factory(NotFoundError()) as client:
            response = await client.get("/boom")

        assert response.json()["trace_id"] == "-"


class TestExceptionHierarchy:
    @pytest.mark.parametrize(
        "exc_cls",
        [StorageUnavailable, NotFoundError, ValidationError, ConflictError],
    )
    def test_all_domain_errors_subclass_app_error(self, exc_cls):
        assert issubclass(exc_cls, AppError)

    def test_app_error_message_defaults_to_class_message(self):
        exc = AppError()
        assert str(exc) == "Internal server error"

    def test_app_error_message_can_be_overridden(self):
        exc = ValidationError("custom message")
        assert exc.message == "custom message"
        assert str(exc) == "custom message"
