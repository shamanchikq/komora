"""Shared plumbing for the scripts that talk to the live Silpo server.

Both `verify_mcp.py` and `smoke_e2e.py` need the same three things: a console that can
print Ukrainian, a check that records its result, and an exit code derived from what
actually happened rather than from where the script stopped.
"""

import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from komora.core.mcp.sanitize import sanitize

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "mcp"

# Product names are Ukrainian and the Windows console defaults to cp1252, which would
# raise UnicodeEncodeError partway through — potentially between adding and removing
# a probe item from a real cart.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError):
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "  ->"

_results: list[tuple[str, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    _results.append((PASS if ok else FAIL, label))
    print(f"[{PASS if ok else FAIL}] {label}" + (f"\n{INFO} {detail}" if detail else ""))
    return ok


def skip(label: str, why: str) -> None:
    """Recorded but not counted as a pass. A skipped check must never read as green."""
    _results.append((SKIP, label))
    print(f"[{SKIP}] {label}\n{INFO} {why}")


def note(text: str) -> None:
    print(f"{INFO} {text}")


def summarise() -> int:
    """Exit code from what actually happened.

    Every exit path goes through here, so a failed check can never be reported as
    success — the first run of `verify_mcp` printed "8 passed, 0 failed" while two
    calls had returned validation errors.
    """
    failures = [label for status, label in _results if status == FAIL]
    skipped = [label for status, label in _results if status == SKIP]
    passed = len(_results) - len(failures) - len(skipped)

    print("\n" + "=" * 70)
    print(f"{passed} passed, {len(failures)} failed, {len(skipped)} skipped")
    for label in failures:
        print(f"  FAILED:  {label}")
    for label in skipped:
        print(f"  SKIPPED: {label}")
    print("=" * 70)
    return 1 if failures else 0


def dump(name: str, payload: Any, *, scrub: bool = True) -> None:
    """Write a fixture. `scrub=False` only for payloads that are pure API definitions.

    Tool schemas contain no user data, and running them through the sanitizer is
    actively harmful: `silpo_find_address` declares a property named `address`, which
    key-based redaction destroys.
    """
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / f"{name}.json"
    path.write_text(
        json.dumps(
            sanitize(payload) if scrub else payload, ensure_ascii=False, indent=2, default=str
        )
        + "\n",
        encoding="utf-8",
    )
    shown = path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path
    print(f"{INFO} wrote {shown}")
