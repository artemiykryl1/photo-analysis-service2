"""Behavioural coverage of the TASK-002 additions to
`app.repositories.photo_repository.PhotoRepository`: `claim_for_processing`,
`record_attempt`, `mark_done`, `mark_failed`, `fetch_unpublished`,
`mark_published`, `count_pending`.

Same style as tests/test_photo_repository.py (TASK-001): `AsyncSession` is
mocked and the compiled SQL text (`literal_binds=True`) is asserted against
- no live Postgres connection. The `WHERE status='pending'` predicate on
`claim_for_processing` is ЗАФИКСИРОВАНО (design §5.3/§12) and gets its own
dedicated compiled-SQL assertion so a future refactor cannot silently widen
or narrow it.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.repositories.photo_repository import PhotoRepository


def _mock_session_with_rowcount(rowcount: int) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.rowcount = rowcount
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


def _mock_session() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    return session


class TestClaimForProcessing:
    async def test_returns_rowcount_from_update_result(self):
        session = _mock_session_with_rowcount(1)
        repo = PhotoRepository()

        result = await repo.claim_for_processing(session, uuid.uuid4())

        assert result == 1

    async def test_returns_zero_when_no_row_matched(self):
        session = _mock_session_with_rowcount(0)
        repo = PhotoRepository()

        result = await repo.claim_for_processing(session, uuid.uuid4())

        assert result == 0

    async def test_predicate_is_pending_status_and_target_photo_id(self):
        """ЗАФИКСИРОВАНО (design §5.3, §12): `WHERE photo_id=:id AND
        status='pending'` must not change - a future refactor that widens
        or narrows this predicate breaks worker idempotency."""
        session = _mock_session_with_rowcount(1)
        repo = PhotoRepository()
        photo_id = uuid.uuid4()

        await repo.claim_for_processing(session, photo_id)

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "UPDATE photos SET status='processing'" in compiled
        assert "photos.status = 'pending'" in compiled
        assert photo_id.hex in compiled

    async def test_does_not_commit_itself(self):
        """Commit is the caller's responsibility (AnalysisProcessor)."""
        session = _mock_session_with_rowcount(1)
        repo = PhotoRepository()

        await repo.claim_for_processing(session, uuid.uuid4())

        session.commit.assert_not_called()


class TestRecordAttempt:
    async def test_writes_attempts_and_error_fields(self):
        session = _mock_session()
        repo = PhotoRepository()
        photo_id = uuid.uuid4()

        await repo.record_attempt(session, photo_id, 2, "UNAVAILABLE", "connection refused")

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "UPDATE photos SET" in compiled
        assert "attempts=2" in compiled.replace(" ", "")
        assert "UNAVAILABLE" in compiled
        assert photo_id.hex in compiled


class TestMarkDone:
    async def test_sets_status_done_and_clears_last_error_fields(self):
        """Review-1 fix M3: mark_done clears last_error_code/last_error_message
        so a photo that failed once (retryable) then succeeded doesn't keep a
        stale error on the `done` row."""
        session = _mock_session()
        repo = PhotoRepository()
        photo_id = uuid.uuid4()

        await repo.mark_done(session, photo_id, attempts=2)

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "UPDATE photos SET" in compiled
        assert "last_error_code=NULL" in compiled.replace(" ", "")
        assert "last_error_message=NULL" in compiled.replace(" ", "")
        assert photo_id.hex in compiled


class TestMarkFailed:
    async def test_sets_status_failed_with_error_fields_and_attempts(self):
        session = _mock_session()
        repo = PhotoRepository()
        photo_id = uuid.uuid4()

        await repo.mark_failed(session, photo_id, "INVALID_ARGUMENT", "bad image", attempts=1)

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "UPDATE photos SET" in compiled
        assert "INVALID_ARGUMENT" in compiled
        assert "attempts=1" in compiled.replace(" ", "")
        assert photo_id.hex in compiled


class TestFetchUnpublished:
    async def test_filters_by_not_sent_and_orders_by_created_at(self):
        session = _mock_session()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        session.execute.return_value = result_mock
        repo = PhotoRepository()

        await repo.fetch_unpublished(session, limit=50)

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "not_sent" in compiled
        assert "ORDER BY photos.created_at" in compiled
        assert "LIMIT 50" in compiled

    async def test_returns_rows_from_scalars_all(self):
        session = _mock_session()
        rows = [MagicMock(), MagicMock()]
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = rows
        session.execute.return_value = result_mock
        repo = PhotoRepository()

        result = await repo.fetch_unpublished(session, limit=50)

        assert result == rows


class TestMarkPublished:
    async def test_sets_publish_status_sent_and_published_at(self):
        session = _mock_session()
        repo = PhotoRepository()
        photo_id = uuid.uuid4()

        await repo.mark_published(session, photo_id)

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "publish_status='sent'" in compiled.replace(" ", "")
        assert "published_at=" in compiled.replace(" ", "")
        assert photo_id.hex in compiled


class TestCountPending:
    async def test_counts_only_pending_status(self):
        session = _mock_session()
        result_mock = MagicMock()
        result_mock.scalar_one.return_value = 7
        session.execute.return_value = result_mock
        repo = PhotoRepository()

        result = await repo.count_pending(session)

        assert result == 7
        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "count(*)" in compiled.lower()
        assert "'pending'" in compiled
