"""Capped, memory-safe reading of uploaded files from FastAPI `UploadFile`s.

TASK-002.1 (tasks/TASK-002.1/20_design.md F2/F8): the previous
implementation read every uploaded file COMPLETELY into memory
(`await file.read()`) before any validation ran - so a batch of up to 10
files (or a single oversized file) was fully materialized in RAM before
`PhotoService` ever got a chance to reject it. This module fixes that by
enforcing the two cheap, purely-in-memory checks as early as possible:

- **File count** is checked BEFORE a single byte of any file is read
  (`len(files)` costs nothing - the list of `UploadFile` handles already
  exists once FastAPI has parsed the multipart request).
- **Per-file size** is capped by reading at most `MAX_FILE_SIZE_BYTES + 1`
  bytes (`UploadFile.read(size)`) - so an oversized file never gets fully
  read into memory; the moment it is detected the read stops for good
  (this file, and the batch as a whole, is rejected immediately - no
  further files are read either).
- **Running total** across a batch is checked after each file, so the sum
  can never exceed `BATCH_MAX_TOTAL_BYTES` while files are still being
  read - not just after all of them are already in memory.

This module is the only place in `app/api/` allowed to import `UploadFile`
directly and call `.read()` on it - `PhotoService` (business layer) stays
FastAPI-agnostic and receives plain `bytes`, exactly as before F2. The
size/count LIMITS THEMSELVES are still owned by `app.services.photo_service`
(imported here, not redefined) - this module is only the early, memory-safe
enforcement mechanism; `PhotoService.create_photo`/`create_batch` still
re-validate everything (defense in depth, and the single source of truth
for the business rule - see design F2 "Константы НЕ переносим").

F8 (documented boundary, out of scope for TASK-002.1): capping the read
bounds RAM per file (<= `MAX_FILE_SIZE_BYTES + 1` bytes) and per batch
(<= `BATCH_MAX_TOTAL_BYTES`), but it is NOT streaming. Starlette already
spools a multipart body into a `SpooledTemporaryFile` (falling back to disk
past its in-memory threshold) before this code ever runs, and MinIO writes
here are still a single `put_object` call with the full byte string, not a
chunked/streamed upload. True end-to-end streaming (multipart parsing ->
MinIO chunked put_object) would require reworking `app.integrations.storage`
and is a materially larger change - deferred. An early rejection based on
the `Content-Length` header (before multipart parsing) was considered and
NOT implemented either: for a multipart body, `Content-Length` is the size
of the WHOLE request (headers + boundaries + all files), and by the time a
request reaches this endpoint FastAPI/Starlette has already parsed the
multipart body into `UploadFile`s - doing it earlier requires ASGI
middleware ahead of routing, a separate piece of work.
"""

from fastapi import UploadFile

from app.core.errors import BatchSizeError, PayloadTooLargeError
from app.services import photo_service

# TASK-002.1 review-1 fix (m1): import the MODULE, not the individual
# constant names. `from app.services.photo_service import
# MAX_FILE_SIZE_BYTES, ...` would bind each constant's *value* at import
# time - after that, `monkeypatch.setattr(photo_service_module,
# "BATCH_MAX_TOTAL_BYTES", X)` (as `tests/test_photo_service_batch.py`
# does) would silently NOT affect this module, because this module would
# already hold its own separate copy of the old value. Going through the
# module object (`photo_service.MAX_FILE_SIZE_BYTES`, looked up at call
# time) keeps `app.services.photo_service` the single, live source of
# truth for every caller, api-layer included. Tests that need to change a
# limit for the api-path specifically must patch
# `app.services.photo_service.<CONST>` (this module has no copies of its
# own to patch).


async def read_capped_file(file: UploadFile) -> bytes:
    """Read at most `MAX_FILE_SIZE_BYTES + 1` bytes from `file`.

    Reading one byte past the limit is enough to detect an oversized file
    without ever holding the whole (potentially huge) file in memory -
    if the cap is exceeded, this raises `PayloadTooLargeError` (413)
    immediately instead of returning the truncated bytes.
    """
    data = await file.read(photo_service.MAX_FILE_SIZE_BYTES + 1)
    if len(data) > photo_service.MAX_FILE_SIZE_BYTES:
        raise PayloadTooLargeError("File exceeds the maximum allowed size (50 MB)")
    return data


async def read_batch_files(files: list[UploadFile]) -> list[tuple[str | None, bytes]]:
    """Validate file count BEFORE reading anything, then read each file
    capped, tracking a running total against `BATCH_MAX_TOTAL_BYTES`.

    Both checks short-circuit as early as possible: an out-of-range file
    count never touches `.read()` at all; a running total that exceeds the
    batch limit raises immediately, without reading the remaining files.

    Peak memory while this function runs is bounded by (bytes already
    accumulated in `result`, <= `BATCH_MAX_TOTAL_BYTES`) + (the one file
    currently being read, <= `MAX_FILE_SIZE_BYTES + 1`) - i.e. up to
    `BATCH_MAX_TOTAL_BYTES + MAX_FILE_SIZE_BYTES` bytes at the single
    worst instant, not `BATCH_MAX_TOTAL_BYTES` alone.
    """
    if not (photo_service.MIN_BATCH_SIZE <= len(files) <= photo_service.MAX_BATCH_SIZE):
        raise BatchSizeError(
            f"Batch must contain between {photo_service.MIN_BATCH_SIZE} and "
            f"{photo_service.MAX_BATCH_SIZE} files (got {len(files)})"
        )

    result: list[tuple[str | None, bytes]] = []
    total = 0
    for f in files:
        data = await read_capped_file(f)
        total += len(data)
        if total > photo_service.BATCH_MAX_TOTAL_BYTES:
            raise PayloadTooLargeError(
                f"Batch total size exceeds the maximum allowed "
                f"{photo_service.BATCH_MAX_TOTAL_BYTES} bytes"
            )
        result.append((f.filename, data))
    return result
