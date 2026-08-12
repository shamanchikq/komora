"""Is the database the app is about to use actually at the current schema?

This exists because of a failure that looked impossible from the outside:
`alembic upgrade head` said success, `alembic check` said no drift, and the first
basket after the deploy died on `table draft_items has no column named description`.

Alembic had migrated a *different database*. `KOMORA_DATABASE_URL` lives in `.env`,
which `alembic/env.py` did not load, so it fell through to its own default while the
bot read `.env` and opened another file. Both commands were telling the truth about a
database nobody was using.

`alembic/env.py` now loads `.env`, which fixes the cause. This checks the symptom
anyway, at startup, where the message can name the problem — rather than at the first
write, inside a traceback the user sees as silence in a chat window.
"""

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

BACKEND = Path(__file__).resolve().parent.parent.parent
ALEMBIC_INI = BACKEND / "alembic.ini"


class SchemaOutOfDate(RuntimeError):
    """The database exists but predates the code about to use it."""


def _sync_url(database_url: str) -> str:
    """Alembic's inspection runs synchronously; the app's URL is async."""
    return database_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")


def current_and_head(database_url: str) -> tuple[str | None, str | None]:
    """(applied revision, latest revision). `None` for a database never migrated."""
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    head = script.get_current_head()

    engine = create_engine(_sync_url(database_url))
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision(), head
    finally:
        engine.dispose()


def assert_current(database_url: str) -> None:
    """Refuse to start against a stale schema, naming the database and the fix.

    Deliberately does not migrate on its own. Silently altering a database at startup
    is a bad habit to build even where it would be safe, and the whole lesson here is
    that "which database" is the question worth being loud about.
    """
    applied, head = current_and_head(database_url)
    if applied == head:
        return

    raise SchemaOutOfDate(
        f"{database_url} is at {applied or 'no migration at all'}, "
        f"but the code expects {head}.\n"
        f"Run:  uv run alembic upgrade head\n"
        f"Check it targeted this database: alembic/env.py reads KOMORA_DATABASE_URL "
        f"from backend/.env."
    )
