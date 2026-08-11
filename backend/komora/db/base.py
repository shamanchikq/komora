"""Engine, session factory, and the declarative base."""

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, Dialect, TypeDecorator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp that is always timezone-aware UTC on the way in and out.

    SQLite has no timezone storage, so a naive datetime written here comes back naive
    and silently reads as local time. The habits engine computes purchase intervals
    from these values, so that drift would be a correctness bug, not a cosmetic one.
    Postgres keeps the offset natively; this makes both behave identically.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"naive datetime reached the database: {value!r}; use utcnow()")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    """The only clock the persistence layer should use."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: UtcDateTime}


def make_engine(url: str, **kwargs: Any) -> AsyncEngine:
    return create_async_engine(url, **kwargs)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False so returned objects stay usable after the session closes.
    return async_sessionmaker(engine, expire_on_commit=False)
