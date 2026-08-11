"""Shared test fixtures."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from komora.db.base import Base, make_session_factory


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """An in-memory database that survives across sessions.

    StaticPool keeps a single connection: without it every session would get its own
    blank `:memory:` database and nothing would persist between calls.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker:
    return make_session_factory(engine)
