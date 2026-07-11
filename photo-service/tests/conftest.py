"""Shared pytest fixtures.

`client` drives the ASGI app directly via httpx without any real
Postgres/MinIO connection - sufficient for `/healthz` (no external
dependency) and for exercising `/readyz`/exception handlers with mocked
dependencies. Full DB/MinIO integration fixtures belong to TASK-001+.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
