"""Behavioural coverage of `app.api.uploads` (TASK-002.1 F2/F8).

Focus: the two cheap checks that must run BEFORE any (potentially huge)
byte string is materialized in memory - file count (checked before a
single `.read()`) and per-file/running-total size caps (checked as bytes
are read, so an oversized file or an over-quota batch never gets fully
read). A minimal fake `UploadFile` stand-in (`_FakeUploadFile`) records
every `.read(size)` call so tests can assert exactly how many times, and
with what argument, each file was read - this is the only way to prove
"read at most N bytes" / "never read this file at all" rather than just
"the final bytes happen to be capped".

Also covers the review-1 m1 late-binding trap: `app.api.uploads` must
look up `photo_service.<CONST>` on the MODULE at call time (not bind the
constant's value at import time), so `monkeypatch.setattr(photo_service,
"<CONST>", X)` - the pattern already used by
`tests/test_photo_service_batch.py` for the service layer - also affects
the api-layer helpers exercised here.
"""

import pytest

from app.api.uploads import read_batch_files, read_capped_file
from app.core.errors import BatchSizeError, PayloadTooLargeError
from app.services import photo_service

JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 20


class _FakeUploadFile:
    """Minimal stand-in for FastAPI's `UploadFile`: `.read(size)` behaves
    like a file object being consumed (subsequent reads return what's
    left), and every call (with its requested `size`) is recorded."""

    def __init__(self, data: bytes, filename: str = "a.jpg"):
        self._data = data
        self.filename = filename
        self.read_calls: list[int | None] = []

    async def read(self, size: int | None = None) -> bytes:
        self.read_calls.append(size)
        if size is None:
            result, self._data = self._data, b""
        else:
            result, self._data = self._data[:size], self._data[size:]
        return result


class TestReadCappedFile:
    async def test_reads_at_most_max_plus_one_bytes(self, monkeypatch):
        monkeypatch.setattr(photo_service, "MAX_FILE_SIZE_BYTES", 10)
        f = _FakeUploadFile(b"x" * 5)

        result = await read_capped_file(f)

        assert result == b"x" * 5
        assert f.read_calls == [11]  # MAX + 1, never "read everything"

    async def test_oversized_file_raises_payload_too_large_without_reading_more(
        self, monkeypatch
    ):
        monkeypatch.setattr(photo_service, "MAX_FILE_SIZE_BYTES", 10)
        f = _FakeUploadFile(b"x" * 1000)

        with pytest.raises(PayloadTooLargeError):
            await read_capped_file(f)

        # exactly one read call, capped at MAX+1 - the other 989 bytes of
        # the (hypothetically huge) file are never touched.
        assert f.read_calls == [11]

    async def test_file_exactly_at_the_limit_is_accepted(self, monkeypatch):
        monkeypatch.setattr(photo_service, "MAX_FILE_SIZE_BYTES", 10)
        f = _FakeUploadFile(b"x" * 10)

        result = await read_capped_file(f)

        assert result == b"x" * 10

    async def test_late_binding_honors_a_patch_on_the_photo_service_module(self, monkeypatch):
        """review-1 m1: `uploads.py` must read `photo_service.MAX_FILE_SIZE_BYTES`
        at call time (via the module object), not bind its value once at
        import time - otherwise patching the service's constant (the
        single source of truth) would silently not affect the api-layer
        cap."""
        monkeypatch.setattr(photo_service, "MAX_FILE_SIZE_BYTES", 3)
        f = _FakeUploadFile(b"xxxx")

        with pytest.raises(PayloadTooLargeError):
            await read_capped_file(f)


class TestReadBatchFilesCountCheckedBeforeAnyRead:
    async def test_eleven_files_rejected_before_a_single_read(self):
        files = [_FakeUploadFile(JPEG_BYTES) for _ in range(11)]

        with pytest.raises(BatchSizeError):
            await read_batch_files(files)

        assert all(f.read_calls == [] for f in files)

    async def test_one_file_rejected_before_a_single_read(self):
        files = [_FakeUploadFile(JPEG_BYTES)]

        with pytest.raises(BatchSizeError):
            await read_batch_files(files)

        assert files[0].read_calls == []

    async def test_zero_files_rejected_before_a_single_read(self):
        with pytest.raises(BatchSizeError):
            await read_batch_files([])

    async def test_two_to_ten_files_pass_the_count_check(self):
        files = [_FakeUploadFile(JPEG_BYTES) for _ in range(2)]

        result = await read_batch_files(files)

        assert len(result) == 2
        assert all(f.read_calls for f in files)  # count check passed - reads did happen


class TestReadBatchFilesPerFileCap:
    async def test_oversized_file_stops_the_batch_and_leaves_later_files_unread(
        self, monkeypatch
    ):
        monkeypatch.setattr(photo_service, "MAX_FILE_SIZE_BYTES", 10)
        oversized = _FakeUploadFile(b"x" * 1000)
        untouched_1 = _FakeUploadFile(b"y" * 5)
        untouched_2 = _FakeUploadFile(b"z" * 5)

        with pytest.raises(PayloadTooLargeError):
            await read_batch_files([oversized, untouched_1, untouched_2])

        assert oversized.read_calls == [11]  # exactly one capped read
        assert untouched_1.read_calls == []
        assert untouched_2.read_calls == []

    async def test_oversized_file_in_the_middle_still_stops_files_after_it(self, monkeypatch):
        monkeypatch.setattr(photo_service, "MAX_FILE_SIZE_BYTES", 10)
        first_ok = _FakeUploadFile(b"a" * 5)
        oversized = _FakeUploadFile(b"x" * 1000)
        untouched = _FakeUploadFile(b"z" * 5)

        with pytest.raises(PayloadTooLargeError):
            await read_batch_files([first_ok, oversized, untouched])

        assert first_ok.read_calls == [11]
        assert oversized.read_calls == [11]
        assert untouched.read_calls == []


class TestReadBatchFilesRunningTotal:
    async def test_running_total_over_the_batch_limit_stops_remaining_files_from_being_read(
        self, monkeypatch
    ):
        monkeypatch.setattr(photo_service, "BATCH_MAX_TOTAL_BYTES", 100)
        files = [
            _FakeUploadFile(b"x" * 40),
            _FakeUploadFile(b"y" * 40),
            _FakeUploadFile(b"z" * 40),  # running total 120 > 100 here
            _FakeUploadFile(b"w" * 40),  # must never be read
        ]

        with pytest.raises(PayloadTooLargeError):
            await read_batch_files(files)

        assert files[0].read_calls and files[1].read_calls and files[2].read_calls
        assert files[3].read_calls == []

    async def test_total_at_or_under_the_limit_is_accepted(self, monkeypatch):
        monkeypatch.setattr(photo_service, "BATCH_MAX_TOTAL_BYTES", 100)
        files = [_FakeUploadFile(b"x" * 40), _FakeUploadFile(b"y" * 40)]

        result = await read_batch_files(files)

        assert [len(data) for _, data in result] == [40, 40]

    async def test_production_constants_reject_four_50mb_files(self):
        """Regression guard for review-1 BLK-2/M2: `BATCH_MAX_TOTAL_BYTES`
        must be an independent 150 MB ceiling, NOT `MAX_BATCH_SIZE *
        MAX_FILE_SIZE_BYTES` (which would make this check unreachable at
        exactly 500 MB, per review-1's proof). Uses the real, unpatched
        production constants deliberately - if the derivation ever
        regresses back to the dead-code product, this test starts failing
        instead of silently passing."""
        size_50mb = 50 * 1024 * 1024
        files = [_FakeUploadFile(b"\x00" * size_50mb) for _ in range(4)]

        with pytest.raises(PayloadTooLargeError):
            await read_batch_files(files)

        # 50 + 50 + 50 = 150 MB does not exceed 150 MB - the 4th file
        # (bringing the running total to 200 MB) is what actually trips
        # the guard, and is therefore still read (the sum can't be known
        # without reading it) - there is simply no 5th file left to prove
        # "unread" here (see the monkeypatched test above for that).
        assert all(f.read_calls == [size_50mb + 1] for f in files)

    async def test_filenames_and_bytes_are_forwarded_as_tuples(self):
        files = [_FakeUploadFile(b"abc", filename="a.jpg"), _FakeUploadFile(b"defg", filename="b.png")]

        result = await read_batch_files(files)

        assert result == [("a.jpg", b"abc"), ("b.png", b"defg")]
