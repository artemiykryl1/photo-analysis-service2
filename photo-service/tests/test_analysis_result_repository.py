"""Behavioural coverage of
`app.repositories.analysis_result_repository.AnalysisResultRepository`.

`upsert` uses `INSERT ... ON CONFLICT (photo_id) DO NOTHING` - the
idempotency guarantee that makes a re-delivered Kafka message safe to
terminal-write twice (design §2.1/§5.3, §13.2 "Идемпотентность worker").
`AsyncSession` is mocked; the compiled SQL is asserted against (no live
Postgres connection), matching the style of test_photo_repository.py.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.repositories.analysis_result_repository import AnalysisResultRepository


def _mock_session() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    return session


class TestUpsert:
    async def test_executes_insert_with_all_fields(self):
        session = _mock_session()
        repo = AnalysisResultRepository()
        photo_id = uuid.uuid4()

        await repo.upsert(
            session,
            photo_id,
            faces_count=3,
            is_blurred=True,
            blur_score=0.75,
            perceptual_hash="deadbeef",
        )

        session.execute.assert_awaited_once()
        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "INSERT INTO analysis_results" in compiled
        assert str(photo_id) in compiled
        assert "3" in compiled
        assert "0.75" in compiled
        assert "deadbeef" in compiled

    async def test_uses_on_conflict_do_nothing_keyed_by_photo_id(self):
        """This is the idempotency guarantee - a re-delivered/re-processed
        message must not overwrite (or IntegrityError on) an existing
        result row."""
        session = _mock_session()
        repo = AnalysisResultRepository()

        await repo.upsert(
            session,
            uuid.uuid4(),
            faces_count=0,
            is_blurred=False,
            blur_score=0.0,
            perceptual_hash="x",
        )

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "ON CONFLICT (photo_id) DO NOTHING" in compiled

    async def test_does_not_commit_itself(self):
        """Commit is the caller's responsibility (AnalysisProcessor)."""
        session = _mock_session()
        repo = AnalysisResultRepository()

        await repo.upsert(
            session,
            uuid.uuid4(),
            faces_count=1,
            is_blurred=False,
            blur_score=0.1,
            perceptual_hash="y",
        )

        session.commit.assert_not_called()
