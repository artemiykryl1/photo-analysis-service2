"""Behavioural coverage of `app.repositories.batch_repository.BatchRepository`.

Same style as tests/test_photo_repository.py: `AsyncSession` is mocked, no
live Postgres connection. `mark_completed`'s compiled SQL is asserted
against directly; `get_by_id` and `create` are asserted at the
session-interaction level (session.get/add/flush call shape) since
`session.get(...)` does not build an inspectable `Select`/`Update`
construct the way `session.execute(select(...))` does.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.db.models import Batch
from app.repositories.batch_repository import BatchRepository


def _mock_session() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    session.get = AsyncMock()
    return session


class TestCreate:
    async def test_adds_and_flushes_without_committing(self):
        session = _mock_session()
        batch = Batch(batch_id=uuid.uuid4(), status="processing")
        repo = BatchRepository()

        result = await repo.create(session, batch)

        session.add.assert_called_once_with(batch)
        session.flush.assert_awaited_once()
        assert result is batch
        assert not hasattr(session, "commit") or not session.commit.called


class TestGetById:
    async def test_returns_none_when_batch_missing(self):
        session = _mock_session()
        session.get.return_value = None
        repo = BatchRepository()

        result = await repo.get_by_id(session, uuid.uuid4())

        assert result is None

    async def test_returns_the_matching_batch(self):
        session = _mock_session()
        batch = Batch(batch_id=uuid.uuid4(), status="processing")
        session.get.return_value = batch
        repo = BatchRepository()

        result = await repo.get_by_id(session, batch.batch_id)

        assert result is batch

    async def test_looks_up_by_batch_model_and_id_with_eager_load_options(self):
        session = _mock_session()
        session.get.return_value = None
        repo = BatchRepository()
        batch_id = uuid.uuid4()

        await repo.get_by_id(session, batch_id)

        session.get.assert_awaited_once()
        call = session.get.call_args
        assert call.args[0] is Batch
        assert call.args[1] == batch_id
        assert "options" in call.kwargs
        assert len(call.kwargs["options"]) == 1  # selectinload(Batch.photos).selectinload(Photo.analysis)


class TestMarkCompleted:
    async def test_sets_status_completed_and_best_photo_id(self):
        session = _mock_session()
        repo = BatchRepository()
        batch_id = uuid.uuid4()
        best_photo_id = uuid.uuid4()

        await repo.mark_completed(session, batch_id, best_photo_id)

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "UPDATE batches SET" in compiled
        assert "status='completed'" in compiled.replace(" ", "")
        assert best_photo_id.hex in compiled
        assert batch_id.hex in compiled

    async def test_accepts_none_best_photo_id_when_all_photos_failed(self):
        session = _mock_session()
        repo = BatchRepository()
        batch_id = uuid.uuid4()

        await repo.mark_completed(session, batch_id, None)

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "best_photo_id=NULL" in compiled.replace(" ", "")

    async def test_does_not_commit_itself(self):
        session = _mock_session()
        repo = BatchRepository()

        await repo.mark_completed(session, uuid.uuid4(), uuid.uuid4())

        session.commit.assert_not_called()
