"""Regression tests for reviewer-1 B1 (tasks/TASK-001/40_review-1.md):

`GET /v1/photos/{photo_id}/content` used to build `Content-Disposition`
from a raw, unescaped filename. A non-ASCII filename (e.g. Cyrillic, a
normal case for a Russian-language product) raised `UnicodeEncodeError`
when Starlette encoded the header as latin-1, turning a valid request
into an unhandled 500. A `"` in the filename also broke the header
syntax. Fixed via an RFC 6266/5987 ASCII fallback + `filename*=UTF-8''...`
extended parameter (see `app.api.photos._content_disposition`).
"""

import uuid
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient

from app.api.photos import _content_disposition, get_photo_service
from app.db.session import get_session
from app.main import app


class TestContentDispositionHelper:
    def test_ascii_filename_is_passed_through(self):
        header = _content_disposition("photo.jpg")
        assert header == "inline; filename=\"photo.jpg\"; filename*=UTF-8''photo.jpg"

    def test_cyrillic_filename_does_not_raise_and_has_ascii_fallback(self):
        header = _content_disposition("фото.jpg")
        # Must be encodable as latin-1 (what Starlette/ASGI does with header values).
        header.encode("latin-1")
        assert 'filename="' in header
        assert "фото" not in header.split(";")[1]  # ascii fallback param has no cyrillic
        assert "filename*=UTF-8''%D1%84%D0%BE%D1%82%D0%BE.jpg" in header

    def test_quote_in_filename_does_not_break_header_syntax(self):
        header = _content_disposition('a"; x="b.jpg')
        # The only two `"` in the whole header must be the opening/closing
        # quote of the `filename="..."` parameter - a stray `"` anywhere
        # else would break the Content-Disposition grammar for clients.
        assert header.count('"') == 2
        header.encode("latin-1")  # must never raise

    def test_backslash_in_filename_is_stripped_from_ascii_fallback(self):
        header = _content_disposition('weird\\name.jpg')
        ascii_param = header.split(";")[1].strip()
        assert "\\" not in ascii_param

    def test_fully_non_ascii_filename_falls_back_to_download(self):
        header = _content_disposition("фото.jpg".replace(".jpg", "") + "фото")
        assert 'filename="download"' in header


class TestDownloadContentEndpointWithNonAsciiFilename:
    async def test_cyrillic_filename_returns_200_not_500(self, monkeypatch):
        photo_id = uuid.uuid4()
        fake_service = AsyncMock()
        fake_service.get_photo_content.return_value = (b"bytes", "image/jpeg", "фото.jpg")

        app.dependency_overrides[get_photo_service] = lambda: fake_service
        app.dependency_overrides[get_session] = lambda: AsyncMock()
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(f"/v1/photos/{photo_id}/content")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        assert response.content == b"bytes"
        disposition = response.headers["content-disposition"]
        assert "filename*=UTF-8''%D1%84%D0%BE%D1%82%D0%BE.jpg" in disposition

    async def test_quote_in_filename_returns_200_with_sane_header(self, monkeypatch):
        photo_id = uuid.uuid4()
        fake_service = AsyncMock()
        fake_service.get_photo_content.return_value = (b"bytes", "image/jpeg", 'a"; x="b.jpg')

        app.dependency_overrides[get_photo_service] = lambda: fake_service
        app.dependency_overrides[get_session] = lambda: AsyncMock()
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(f"/v1/photos/{photo_id}/content")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        disposition = response.headers["content-disposition"]
        assert disposition.count('"') == 2
