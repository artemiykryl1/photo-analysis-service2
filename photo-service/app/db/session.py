"""Async database engine, session factory and FastAPI dependency.

Commit/rollback boundaries are NOT managed here - `get_session` only
yields a session; the calling layer (services/) is responsible for
committing or rolling back within its own unit of work.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=5,
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an AsyncSession.

    Commit/rollback is the responsibility of the calling layer (services/).
    """
    async with SessionLocal() as session:
        yield session
