"""Capture the purchase-history tools against a Silpo account that actually shops.

Read-only. Exists because the habits engine (Plan 3) is designed around payloads nobody
has seen: on 2026-09-13 all three history tools answered `total: 0` for the development
account, which holds a real loyalty card and has simply never bought anything. See
`docs/silpo-mcp-reference.md` §9 for what that run established and what it could not.

**It signs in as its own operator, not as a Telegram user.** The bot's users table is
left alone, no tunnel is needed (Silpo accepts the loopback redirect, §7), and the
account used here need not be the one linked to the bot.

**The account may not be yours.** History is the most personal payload Silpo serves —
not merely a name and a phone, which `core/mcp/sanitize.py` redacts by key, but the
record of what a person eats and when. Two consequences, both defaults here:

  - Tokens are FORGOTTEN when the run ends (`--keep-tokens` to override), so a borrowed
    account does not stay linked to a development database.
  - Fixtures are written only with `--write-fixtures`. The default prints the *shape* —
    keys, types, counts, the fields the habits engine needs — which is what the design
    questions actually require. Before committing a fixture from a real shopper, cut it
    down to a couple of representative lines: the sanitizer scrubs identity, not the
    shopping list.

Whoever owns the account must be the one to sign in at the printed URL.

**Run it with the same `KOMORA_PUBLIC_BASE_URL` the bot is using.** The DCR registration
is one app-wide row: this script may register (it is a login path, so the callback check
is on), and registering afresh against a different callback invalidates the refresh of
every account already linked — they are asked to link again, with no error to explain
it. That is not hypothetical; it is how 2026-09-13 began.

USAGE
    uv run python scripts/capture_history.py
    uv run python scripts/capture_history.py --write-fixtures --keep-tokens
    uv run python scripts/capture_history.py --user <id> --full --since 2024-01-01 --measure

The last form is Task 0 re-run: it reads as an account already linked to the bot, pages
through the whole history with `dateStart` pushed back, and prints the habits measure
(`_habits_measure.py`) as counts — no product is named.
"""

import argparse
import asyncio
import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import uvicorn
from _habits_measure import summary
from _report import INFO, check, dump, summarise
from dotenv import load_dotenv

from komora.api.app import create_app
from komora.core.crypto import TokenCipher
from komora.core.mcp.auth import (
    AuthorizationBridge,
    DBTokenStorage,
    PersistentOAuthClientProvider,
    build_client_metadata,
)
from komora.core.mcp.client import open_session
from komora.core.mcp.payload import error_of, unwrap
from komora.core.mcp.sanitize import sanitize
from komora.db.base import Base, make_engine, make_session_factory
from komora.db.repo import OAuthClientRepo, UserRepo

LOCAL_USER = 1  # this script serves a single operator, as verify_mcp.py does

OFFLINE_MAX_LIMIT = 10
ONLINE_MAX_LIMIT = 50
"""`silpo_get_my_offline_orders` declares `max: 10` and enforces it with -32602.
The online tool says 100 in the August fixture and enforces **50** live (reference §9).
Reading the schema would have saved a round trip."""

CATEGORY_HINTS = ("categor",)
"""Matched inside a key's last segment. `section` and `group` were here once, and `group`
matched `rewards.rewardGroupCodeName` — a loyalty reward group — as a product category."""
DATE_HINTS = ("date", "createdat", "orderdate", "purchasedat", "time", "timestamp")


def cart_context(cart: Any) -> dict[str, str] | None:
    """Branch, delivery type and timeslot — required by the offline-orders tool.

    All four live on the cart, which is why in-store history cannot be read without a
    branch and an unexpired slot. That coupling is the constraint a nightly import job
    inherits; it is not an accident of this script.
    """
    # The response wraps the cart as {"success": true, "cart": {...}} — the key
    # `pipeline._cart_body` and `verify_mcp.cart_body` both read. This line first
    # guessed `shoppingCart`, found nothing, and reported a slot that was set as
    # missing, twice, against a real account.
    inner = cart.get("cart") if isinstance(cart, dict) else None
    body = inner if isinstance(inner, dict) else (cart if isinstance(cart, dict) else {})
    shipments = [s for s in (body.get("shipments") or []) if isinstance(s, dict)]
    timeslot = body.get("timeslot") or {}
    fields = {
        "branchId": shipments[0].get("branchId") if shipments else None,
        "deliveryType": body.get("deliveryType"),
        "timeslotStart": timeslot.get("start"),
        "timeslotEnd": timeslot.get("end"),
    }
    if not all(fields.values()):
        return None
    return {k: str(v) for k, v in fields.items()}


def describe(name: str, payload: Any) -> None:
    """Print the structure the habits design turns on, without printing the groceries.

    A count is reported, never asserted: what an account holds is decided by its owner's
    shopping, not by this script.
    """
    if not isinstance(payload, dict):
        print(f"{INFO} {name}: not an object — {type(payload).__name__}")
        return

    items = next(
        (
            payload[k]
            for k in ("orders", "products", "receipts")
            if isinstance(payload.get(k), list)
        ),
        [],
    )
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    print(
        f"{INFO} {name}: {len(items)} entr(y/ies), meta={meta}, summary={payload.get('summary')!r}"
    )
    if not items:
        print("      empty — the populated shape stays unknown (reference §9)")
        return

    first = items[0]
    print("      shape of the first entry (keys and types only — no values):")
    for line in skeleton(first):
        print(f"        {line}")

    # The two questions Plan 3 cannot be written without. Searched at every depth: the
    # tool description puts `catalogProduct` — an object — on each line, and a category
    # nested inside it is still a category.
    paths = list(key_paths(first))
    cats = [p for p, _ in paths if any(h in p.rsplit(".", 1)[-1].lower() for h in CATEGORY_HINTS)]
    dates = [
        (p, v)
        for p, v in paths
        if isinstance(v, str | int | float)
        and any(h in p.rsplit(".", 1)[-1].lower() for h in DATE_HINTS)
    ]
    check(f"{name}: a category field exists somewhere", bool(cats), f"found {cats}")
    check(
        f"{name}: a date field exists somewhere",
        bool(dates),
        ", ".join(f"{p} like {date_format(v)!r}" for p, v in dates),
    )


def skeleton(value: Any, depth: int = 0) -> list[str]:
    """Keys and types, recursively. A list shows its length and its first element's shape.

    Types only, so the output can be read — and pasted — without exposing what anyone
    bought, when, or for how much.
    """
    pad = "  " * depth
    out: list[str] = []
    if isinstance(value, dict):
        for k in sorted(value):
            v = value[k]
            if isinstance(v, dict | list) and v:
                label = f"list[{len(v)}]" if isinstance(v, list) else "object"
                out.append(f"{pad}{k}: {label}")
                inner = v[0] if isinstance(v, list) else v
                if isinstance(inner, dict | list):
                    out.extend(skeleton(inner, depth + 1))
                else:
                    out.append(f"{pad}  [0]: {type(inner).__name__}")
            else:
                out.append(f"{pad}{k}: {'null' if v is None else type(v).__name__}")
    elif isinstance(value, list) and value:
        out.extend(skeleton(value[0], depth))
    return out


def key_paths(value: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Every `a.b.c` path in the first element of each list, with its leaf value."""
    if isinstance(value, dict):
        for k, v in value.items():
            path = f"{prefix}.{k}" if prefix else k
            yield path, v
            yield from key_paths(v, path)
    elif isinstance(value, list) and value:
        yield from key_paths(value[0], prefix)


def date_format(value: Any) -> str:
    """The format of a timestamp without the timestamp: every digit becomes 9.

    Enough to tell ISO-8601 from epoch seconds from a local date string — which the
    median-interval rule has to parse — and nothing about when anyone shopped.
    """
    if isinstance(value, str):
        return "".join("9" if c.isdigit() else c for c in value)
    return type(value).__name__


async def fetch_all(call: Any, tool: str, args: dict[str, Any], *, page: int) -> dict[str, Any]:
    """Page through a history tool until `meta.total` is reached.

    Both tools page by `offset`; the online one caps a page at 50 live (100 in the
    August fixture), the offline one at 10. The merged payload keeps the envelope of
    the first page so the same code reads a full history and a single page alike.
    """
    first = await call(tool, {**args, "limit": page, "offset": 0})
    if not isinstance(first, dict):
        return {"orders": []}
    items = list(first.get("orders") or [])
    total = int((first.get("meta") or {}).get("total") or len(items))
    offset = len(items)
    while offset < total and items:
        more = await call(tool, {**args, "limit": page, "offset": offset})
        got = list((more or {}).get("orders") or []) if isinstance(more, dict) else []
        if not got:
            break
        items.extend(got)
        offset += len(got)
    return {**first, "orders": items, "meta": {**(first.get("meta") or {}), "fetched": len(items)}}


def pick_removed(online: dict[str, Any]) -> dict[str, Any] | None:
    """The delivered order with the fewest lines that still has a `removed: true` one.

    The committed online fixture has no removed line, so Task 1's rule for them would be
    tested against an invented one. This finds a real one to trim into the fixture.
    """
    candidates = [
        o
        for o in online.get("orders") or []
        if isinstance(o, dict)
        and any(isinstance(p, dict) and p.get("removed") for p in o.get("products") or [])
    ]
    return min(candidates, key=lambda o: len(o.get("products") or []), default=None)


async def main(
    write_fixtures: bool,
    keep_tokens: bool,
    port: int,
    user: int | None,
    full: bool,
    since: str | None,
    measure: bool,
    pick_removed_to: str | None,
    dump_raw: str | None,
    auth_timeout: float,
) -> int:
    key = os.environ.get("KOMORA_TOKEN_ENCRYPTION_KEY")
    if not key:
        print("KOMORA_TOKEN_ENCRYPTION_KEY is not set — see backend/.env.example.")
        return 2

    server_url = os.environ.get("KOMORA_SILPO_MCP_URL", "https://mcp.silpo.ua/mcp")
    base_url = os.environ.get("KOMORA_PUBLIC_BASE_URL", "http://localhost:8000")
    engine = make_engine(os.environ.get("KOMORA_DATABASE_URL", "sqlite+aiosqlite:///./verify.db"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    users = UserRepo(sessions)

    bridge = AuthorizationBridge(timeout_seconds=auth_timeout)
    server = uvicorn.Server(
        uvicorn.Config(create_app(bridge), host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)

    async def show_url(_: int, url: str) -> None:
        print("\n" + "=" * 70)
        print("THE ACCOUNT OWNER SHOULD OPEN THIS AND SIGN IN TO SILPO:\n")
        print(url)
        print("=" * 70 + "\n")

    async def refuse(_: int, __: str) -> None:
        raise RuntimeError(f"user {user} holds no usable Silpo tokens; this mode never logs in")

    # `--user` reads as an account already linked to the bot, the way `gateway.connect`
    # does: no registration, no login, and the tokens are never forgotten because they
    # are not this script's to forget.
    operator = user if user is not None else LOCAL_USER
    redirect_handler, callback_handler = bridge.handlers(
        operator, refuse if user is not None else show_url
    )
    provider = PersistentOAuthClientProvider(
        server_url=server_url,
        client_metadata=build_client_metadata(base_url),
        storage=DBTokenStorage(
            telegram_id=operator,
            users=users,
            clients=OAuthClientRepo(sessions),
            cipher=TokenCipher(key),
            redirect_uri=None
            if user is not None
            else f"{base_url.rstrip('/')}/auth/silpo/callback",
        ),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    try:
        async with open_session(server_url, provider) as session:

            async def call(tool: str, args: dict[str, Any]) -> Any:
                payload = unwrap(await session.call_tool(tool, args))
                problem = error_of(payload)
                check(f"{tool} answered ({args.get('offset', 0)})", problem is None, problem or "")
                return payload

            online = (
                await fetch_all(call, "silpo_get_my_online_orders", {}, page=ONLINE_MAX_LIMIT)
                if full
                else await call("silpo_get_my_online_orders", {"limit": 10})
            )
            describe("online orders", online)
            if write_fixtures:
                dump("my_online_orders", online)

            cart_id_raw = await call("silpo_get_my_shopping_cart", {})
            cart_id = cart_id_raw.get("shoppingCartId") if isinstance(cart_id_raw, dict) else None
            context = None
            if cart_id:
                context = cart_context(
                    await call("silpo_get_shopping_cart_by_id", {"shoppingCartId": str(cart_id)})
                )
            if not check(
                "cart carries branch + delivery + timeslot",
                context is not None,
                "offline history and favourites need all four; pick a slot in the Silpo app",
            ):
                return summarise()
            assert context is not None

            # `dateStart` defaults to six months ago on the server. Without it, "how far
            # back do receipts go" measures the default window, not the history.
            window = {"dateStart": f"{since}T00:00:00"} if since else {}
            offline = (
                await fetch_all(
                    call,
                    "silpo_get_my_offline_orders",
                    {**context, **window},
                    page=OFFLINE_MAX_LIMIT,
                )
                if full
                else await call(
                    "silpo_get_my_offline_orders",
                    {**context, **window, "limit": OFFLINE_MAX_LIMIT},
                )
            )
            describe("offline orders", offline)
            if write_fixtures:
                dump("my_offline_orders", offline)

            favourites = await call(
                "silpo_get_my_favorites", {k: v for k, v in context.items() if k != "timeslotEnd"}
            )
            describe("favourites", favourites)
            if write_fixtures:
                dump("my_favorites", favourites)

            if measure:
                print("\n" + "-" * 70 + "\nTask 0 measure (counts only):")
                print(summary(online, offline))

            if dump_raw:
                # Sanitised, but still a person's whole shopping record: a scratch
                # directory for one analysis session, deleted afterwards — never a
                # fixture and never committed.
                raw = Path(dump_raw)
                await asyncio.to_thread(raw.mkdir, parents=True, exist_ok=True)
                for name, payload in (
                    ("online", online),
                    ("offline", offline),
                    ("favourites", favourites),
                ):
                    text = json.dumps(sanitize(payload), ensure_ascii=False, indent=1) + "\n"
                    await asyncio.to_thread(
                        (raw / f"{name}.json").write_text, text, encoding="utf-8"
                    )
                print(f"{INFO} raw (sanitised) payloads in {raw} — delete when done")

            if pick_removed_to:
                order = pick_removed(online)
                if check("an order with a removed line exists", order is not None):
                    text = json.dumps(sanitize(order), ensure_ascii=False, indent=2) + "\n"
                    await asyncio.to_thread(
                        Path(pick_removed_to).write_text, text, encoding="utf-8"
                    )
                    print(f"{INFO} wrote {pick_removed_to} — trim into my_online_orders.json")

        return summarise()
    finally:
        if user is None and not keep_tokens:
            await users.clear_tokens(LOCAL_USER)
            print(f"{INFO} tokens forgotten — pass --keep-tokens to stay linked")
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        await engine.dispose()


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-fixtures",
        action="store_true",
        help="write sanitized fixtures; trim them by hand before committing",
    )
    parser.add_argument(
        "--keep-tokens", action="store_true", help="stay linked after the run (default: forget)"
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--user",
        type=int,
        default=None,
        help="read as this already-linked bot user (telegram id); no login, tokens kept",
    )
    parser.add_argument("--full", action="store_true", help="page through the whole history")
    parser.add_argument(
        "--since", default=None, help="offline dateStart, YYYY-MM-DD (server default: 6 months)"
    )
    parser.add_argument(
        "--measure", action="store_true", help="print the Task 0 habits measure (counts only)"
    )
    parser.add_argument(
        "--dump-raw", default=None, help="write sanitised full payloads to this scratch dir"
    )
    parser.add_argument(
        "--auth-timeout",
        type=float,
        default=600.0,
        help="seconds to wait for the account owner to sign in (default: 600)",
    )
    parser.add_argument(
        "--pick-removed",
        dest="pick_removed_to",
        default=None,
        help="write the smallest order carrying a removed line, sanitized, to this path",
    )
    raise SystemExit(asyncio.run(main(**vars(parser.parse_args()))))
