"""Alembic environment.

The database URL is read straight from `KOMORA_DATABASE_URL` rather than through
`Settings`, so migrations run without a bot token or an API key — CI and a fresh
checkout should be able to build the schema with nothing configured.

**`.env` is loaded first, and that is not optional.** Without it this file saw no
`KOMORA_DATABASE_URL` — the variable lives in `.env`, which only the app loads — fell
through to the default below, and migrated a database the bot never opens. Everything
looked right: `alembic upgrade head` reported success, `alembic check` reported no
drift, and the first basket after the deploy died on a missing column.
"""

import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Importing `tables` registers every table on Base.metadata for autogenerate.
from komora.db import tables  # noqa: F401
from komora.db.base import Base

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option(
    "sqlalchemy.url",
    os.environ.get("KOMORA_DATABASE_URL", "sqlite+aiosqlite:///./komora.db"),
)

target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things in place; batch mode rewrites the table.
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
