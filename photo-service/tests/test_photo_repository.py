"""Behavioural coverage of `app.repositories.photo_repository.PhotoRepository`.

Per tasks/TASK-001/20_design.md §11.2, a Postgres-backed round-trip test
would be the most realistic option, but SQLite is not enum/UUID-compatible
with the Postgres-specific column types used by `app.db.models.Photo`
(`postgresql.UUID`, `Enum`), and Docker (needed for a testcontainers
Postgres) is unavailable in this sandbox - see
tasks/TASK-001/50_tests.md "Что покрыто / не покрыто" for the tradeoff.

Instead, `AsyncSession` is mocked here and the actual SQLAlchemy
`Select`/`Update` statements the repository builds are compiled to SQL
text (`literal_binds=True`) and asserted against, which catches the same
class of bug (wrong column, wrong ordering, wrong limit/offset) without a
live database - only real DDL/constraint execution (e.g. the enum
double-CREATE-TYPE class of bug from TASK-000) requires the dedicated
`tests/test_migration_integration.py`.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError
from app.db.models import Photo, PhotoStatus
from app.repositories.photo_repository import PhotoRepository


def _new_photo() -> Photo:
    return Photo(
        photo_id=uuid.uuid4(),
        filename="cat.jpg",
        object_key=f"photos/{uuid.uuid4()}/original.jpg",
        status=PhotoStatus.pending,
    )


def _mock_session() -> MagicMock:
    """A session double where only the methods the repository actually
    `await`s (`flush`/`execute`/`commit`/`rollback`) are async - `add` is a
    plain sync call in real SQLAlchemy, and making it an `AsyncMock` (as a
    blanket `unittest.mock.AsyncMock()` session would) creates an
    "coroutine was never awaited" warning since the repository correctly
    never awaits it.
    """
    session = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


class TestCreate:
    async def test_adds_and_flushes_without_committing(self):
        session = _mock_session()
        photo = _new_photo()
        repo = PhotoRepository()

        result = await repo.create(session, photo)

        session.add.assert_called_once_with(photo)
        session.flush.assert_awaited_once()
        session.commit.assert_not_called()
        assert result is photo

    async def test_integrity_error_on_flush_raises_conflict_error(self):
        session = _mock_session()
        session.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate key"))
        photo = _new_photo()
        repo = PhotoRepository()

        with pytest.raises(ConflictError):
            await repo.create(session, photo)

    async def test_conflict_error_does_not_swallow_add_call(self):
        """Even on a later flush failure, the row must have been `add`-ed
        (this is what makes `session.rollback()` in the service layer
        meaningful)."""
        session = _mock_session()
        session.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate key"))
        photo = _new_photo()
        repo = PhotoRepository()

        with pytest.raises(ConflictError):
            await repo.create(session, photo)

        session.add.assert_called_once_with(photo)


class TestGetById:
    async def test_returns_none_when_row_missing(self):
        session = _mock_session()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock
        repo = PhotoRepository()

        result = await repo.get_by_id(session, uuid.uuid4())

        assert result is None

    async def test_returns_the_matching_row(self):
        session = _mock_session()
        photo = _new_photo()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = photo
        session.execute.return_value = result_mock
        repo = PhotoRepository()

        result = await repo.get_by_id(session, photo.photo_id)

        assert result is photo

    async def test_filters_by_photo_id_primary_key(self):
        session = _mock_session()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock
        repo = PhotoRepository()
        photo_id = uuid.uuid4()

        await repo.get_by_id(session, photo_id)

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "photos.photo_id" in compiled
        # asyncpg/SQLAlchemy renders the UUID literal without dashes.
        assert photo_id.hex in compiled


class TestList:
    async def test_empty_table_returns_empty_list(self):
        session = _mock_session()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        session.execute.return_value = result_mock
        repo = PhotoRepository()

        result = await repo.list(session, limit=50, offset=0)

        assert result == []

    async def test_returns_rows_from_scalars_all(self):
        session = _mock_session()
        rows = [_new_photo(), _new_photo()]
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = rows
        session.execute.return_value = result_mock
        repo = PhotoRepository()

        result = await repo.list(session, limit=50, offset=0)

        assert result == rows

    async def test_orders_by_created_at_descending_with_limit_and_offset(self):
        session = _mock_session()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        session.execute.return_value = result_mock
        repo = PhotoRepository()

        await repo.list(session, limit=10, offset=5)

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "ORDER BY photos.created_at DESC" in compiled
        assert "LIMIT 10" in compiled
        assert "OFFSET 5" in compiled


class TestUpdateStatus:
    async def test_executes_update_and_does_not_commit(self):
        session = _mock_session()
        repo = PhotoRepository()
        photo_id = uuid.uuid4()

        await repo.update_status(session, photo_id, PhotoStatus.done)

        session.execute.assert_awaited_once()
        session.commit.assert_not_called()

    async def test_update_statement_targets_photos_table_and_status_column(self):
        session = _mock_session()
        repo = PhotoRepository()
        photo_id = uuid.uuid4()

        await repo.update_status(session, photo_id, PhotoStatus.failed)

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "UPDATE photos SET" in compiled
        assert "status=" in compiled.replace(" ", "")
        assert photo_id.hex in compiled
