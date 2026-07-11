"""Tests for `app.db.session`.

`get_session` is a thin FastAPI dependency wrapping `SessionLocal`. We
only verify its generator contract (yields exactly one `AsyncSession`-ish
object and closes it) using a patched `SessionLocal`, since exercising
the real `async_sessionmaker` would require a live Postgres connection
(explicitly out of scope for TASK-000 tests).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import app.db.session as session_module
from app.db.session import get_session


async def test_get_session_yields_a_session_and_closes_it():
    fake_session = AsyncMock()
    fake_session_local = MagicMock()
    fake_session_local.return_value.__aenter__.return_value = fake_session
    fake_session_local.return_value.__aexit__.return_value = False

    with patch.object(session_module, "SessionLocal", fake_session_local):
        gen = get_session()
        yielded = await gen.__anext__()
        assert yielded is fake_session

        # Exhaust the generator to trigger the `async with` __aexit__.
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass

    fake_session_local.return_value.__aexit__.assert_awaited()


def test_engine_and_session_local_are_module_level_singletons():
    # `engine`/`SessionLocal` must be created once at import time, not
    # per-request, so connection pooling actually applies.
    import app.db.session as mod

    assert mod.engine is mod.engine
    assert mod.SessionLocal is mod.SessionLocal
