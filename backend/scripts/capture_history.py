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
"""

import argparse
import asyncio
import contextlib
import os
from collections.abc import Iterator
from typing import Any

import uvicorn
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
from komora.db.base import Base, make_engine, make_session_factory
from komora.db.repo import OAuthClientRepo, UserRepo

LOCAL_USER = 1  # this script serves a single operator, as verify_mcp.py does

OFFLINE_MAX_LIMIT = 10
"""`silpo_get_my_offline_orders` declares `max: 10` and enforces it with -32602.
The online tool takes 100. Reading the schema would have saved a round trip."""

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


async def main(write_fixtures: bool, keep_tokens: bool, port: int) -> int:
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

    bridge = AuthorizationBridge()
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

    redirect_handler, callback_handler = bridge.handlers(LOCAL_USER, show_url)
    provider = PersistentOAuthClientProvider(
        server_url=server_url,
        client_metadata=build_client_metadata(base_url),
        storage=DBTokenStorage(
            telegram_id=LOCAL_USER,
            users=users,
            clients=OAuthClientRepo(sessions),
            cipher=TokenCipher(key),
            redirect_uri=f"{base_url.rstrip('/')}/auth/silpo/callback",
        ),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    try:
        async with open_session(server_url, provider) as session:

            async def call(tool: str, args: dict[str, Any]) -> Any:
                payload = unwrap(await session.call_tool(tool, args))
                problem = error_of(payload)
                check(f"{tool} answered", problem is None, problem or "")
                return payload

            online = await call("silpo_get_my_online_orders", {"limit": 10})
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

            offline = await call(
                "silpo_get_my_offline_orders", {**context, "limit": OFFLINE_MAX_LIMIT}
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

        return summarise()
    finally:
        if not keep_tokens:
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
    raise SystemExit(asyncio.run(main(**vars(parser.parse_args()))))
