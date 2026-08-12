"""A trap that has cost four migrations, checked by a test instead of by a live run.

`alembic revision --autogenerate` emits `nullable=False` with no `server_default`
whenever the model declares a Python-side default: the default lives in SQLAlchemy, so
no DDL carries it. The result applies cleanly to an empty database — which is what CI
and `alembic check` see — and fails on the first one with a row in it:

    sqlite3.OperationalError: Cannot add a NOT NULL column with default value NULL

That is a migration whose whole test suite passes and which cannot be run on the only
database that matters. It has happened with `draft_items.description`,
`draft_baskets.coupon_notes` and `draft_baskets.removals`.
"""

import re
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"

ADD_COLUMN = re.compile(r"add_column\(\s*sa\.Column\((?P<args>.*?)\)\s*,?\s*\)", re.DOTALL)


def migrations() -> list[Path]:
    return sorted(p for p in VERSIONS.glob("*.py") if p.name != "__init__.py")


def test_there_are_migrations() -> None:
    """Guards the guard: a glob that matches nothing passes every check below."""
    assert migrations()


@pytest.mark.parametrize("path", migrations(), ids=lambda p: p.stem)
def test_not_null_columns_are_added_with_a_server_default(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    offenders = [
        " ".join(match.group("args").split())
        for match in ADD_COLUMN.finditer(source)
        if "nullable=False" in match.group("args") and "server_default" not in match.group("args")
    ]
    assert not offenders, (
        f"{path.name} adds a NOT NULL column with no server_default: {offenders}. "
        "Autogenerate omits it whenever the model has a Python-side default. It works "
        "on an empty database and fails on one with rows — add server_default by hand."
    )
