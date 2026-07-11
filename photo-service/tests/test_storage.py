"""Tests for `app.integrations.storage.ObjectStorage`.

The real MinIO SDK client is never instantiated/contacted here -
`ObjectStorage._client` is monkeypatched with a `unittest.mock.MagicMock`
after construction, so the tests exercise only:
  - `ensure_bucket` idempotency (bucket_exists=True -> make_bucket NOT
    called; bucket_exists=False -> make_bucket called).
  - `save_file`/`get_file` delegate to the underlying SDK with the
    expected arguments and return values.
  - S3Error -> domain exception mapping (StorageUnavailable / NotFoundError).

`ObjectStorage.__init__` itself constructs a real `minio.Minio(...)`
object, which does not perform any network I/O (the SDK only opens a
connection lazily on the first actual call), so calling the real
constructor with dummy settings is safe and does not require mocking.
"""

from unittest.mock import MagicMock

import pytest
from minio.error import S3Error

from app.core.config import Settings
from app.core.errors import NotFoundError, StorageUnavailable
from app.integrations.storage import ObjectStorage


def _s3_error(code: str = "InternalError") -> S3Error:
    return S3Error(
        response=None,
        code=code,
        message="boom",
        resource="/photos/x",
        request_id="req-1",
        host_id="host-1",
    )


@pytest.fixture
def storage() -> ObjectStorage:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        MINIO_ENDPOINT="minio.invalid:9000",
        MINIO_ACCESS_KEY="test-access",
        MINIO_SECRET_KEY="test-secret",
        MINIO_BUCKET="test-bucket",
        MINIO_SECURE=False,
    )
    obj_storage = ObjectStorage(settings)
    obj_storage._client = MagicMock()
    return obj_storage


class TestEnsureBucketIdempotency:
    def test_bucket_exists_true_does_not_create_bucket(self, storage):
        storage._client.bucket_exists.return_value = True

        storage.ensure_bucket()

        storage._client.bucket_exists.assert_called_once_with("test-bucket")
        storage._client.make_bucket.assert_not_called()

    def test_bucket_exists_false_creates_bucket(self, storage):
        storage._client.bucket_exists.return_value = False

        storage.ensure_bucket()

        storage._client.bucket_exists.assert_called_once_with("test-bucket")
        storage._client.make_bucket.assert_called_once_with("test-bucket")

    def test_ensure_bucket_raises_storage_unavailable_on_s3_error(self, storage):
        storage._client.bucket_exists.side_effect = _s3_error()

        with pytest.raises(StorageUnavailable):
            storage.ensure_bucket()

    def test_ensure_bucket_calling_twice_is_idempotent(self, storage):
        """Second call sees bucket_exists=True (as if the first call created it)."""
        storage._client.bucket_exists.side_effect = [False, True]

        storage.ensure_bucket()
        storage.ensure_bucket()

        assert storage._client.make_bucket.call_count == 1


class TestSaveFile:
    def test_save_file_delegates_to_client_put_object(self, storage):
        data = b"fake-jpeg-bytes"

        storage.save_file(
            "user123/photo-id.jpg", data, length=len(data), content_type="image/jpeg"
        )

        storage._client.put_object.assert_called_once()
        call = storage._client.put_object.call_args
        assert call.args[0] == "test-bucket"
        assert call.args[1] == "user123/photo-id.jpg"
        assert call.kwargs["length"] == len(data)
        assert call.kwargs["content_type"] == "image/jpeg"
        assert call.kwargs["data"].read() == data

    def test_save_file_returns_bucket_prefixed_s3_path(self, storage):
        result = storage.save_file("obj.png", b"x", length=1, content_type="image/png")
        assert result == "test-bucket/obj.png"

    def test_save_file_raises_storage_unavailable_on_s3_error(self, storage):
        storage._client.put_object.side_effect = _s3_error()

        with pytest.raises(StorageUnavailable):
            storage.save_file("obj.jpg", b"x", length=1, content_type="image/jpeg")


class TestGetFile:
    def test_get_file_delegates_to_client_get_object_and_reads_bytes(self, storage):
        mock_response = MagicMock()
        mock_response.read.return_value = b"payload-bytes"
        storage._client.get_object.return_value = mock_response

        result = storage.get_file("user123/photo-id.jpg")

        storage._client.get_object.assert_called_once_with("test-bucket", "user123/photo-id.jpg")
        assert result == b"payload-bytes"

    def test_get_file_closes_and_releases_connection_on_success(self, storage):
        mock_response = MagicMock()
        mock_response.read.return_value = b"data"
        storage._client.get_object.return_value = mock_response

        storage.get_file("obj.jpg")

        mock_response.close.assert_called_once()
        mock_response.release_conn.assert_called_once()

    def test_get_file_missing_object_raises_not_found_error(self, storage):
        storage._client.get_object.side_effect = _s3_error(code="NoSuchKey")

        with pytest.raises(NotFoundError):
            storage.get_file("missing.jpg")

    def test_get_file_other_s3_error_raises_storage_unavailable(self, storage):
        storage._client.get_object.side_effect = _s3_error(code="InternalError")

        with pytest.raises(StorageUnavailable):
            storage.get_file("obj.jpg")
