"""Day-1 live verification against Silpo's MCP server. Run manually.

Proves the assumptions the whole sync design rests on, and captures real response
shapes as test fixtures.

  A1  `silpo_add_or_update_cart_products` APPENDS and upserts by product — it does
      not replace the cart. Inferred from tool naming; never actually observed.
  A2  Real tool JSON Schemas, captured so the Gemini converter (Task 8) can be
      tested against what Silpo actually sends rather than what we imagine.

SAFETY. The A1 probe mutates your real Silpo cart, so it is opt-in via --probe-cart.
It records the cart first, adds the cheapest item it can find, then removes it and
verifies the cart matches the original. `silpo_clear_shopping_cart` is never called.

NO TUNNEL NEEDED. Silpo's Dynamic Client Registration accepts a loopback redirect
(verified 2026-08-10: POST /register with http://localhost:8000/... returned 201), and
a loopback callback registers as a `native` client per RFC 8252. A deployed Komora
still needs a public HTTPS callback; this script does not.

USAGE
    Configuration comes from backend/.env — no shell exports, so this works the same
    in PowerShell and bash. Keeping the encryption key in a file also matters: a key
    regenerated per run cannot decrypt the tokens the previous run stored.

        uv run python scripts/verify_mcp.py                # read-only first
        uv run python scripts/verify_mcp.py --probe-cart   # then the cart probe
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import uvicorn
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
from komora.core.mcp.sanitize import sanitize
from komora.db.base import Base, make_engine, make_session_factory
from komora.db.repo import OAuthClientRepo, UserRepo

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "mcp"
LOCAL_USER = 1  # this script serves a single operator

# Product names are Ukrainian and the Windows console defaults to cp1252, which would
# raise UnicodeEncodeError partway through — potentially between adding and removing
# the probe item.
for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

PASS, FAIL, INFO = "PASS", "FAIL", "  ->"
_results: list[tuple[str, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    _results.append((PASS if ok else FAIL, label))
    print(f"[{PASS if ok else FAIL}] {label}" + (f"\n{INFO} {detail}" if detail else ""))
    return ok


def dump(name: str, payload: Any) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / f"{name}.json"
    path.write_text(
        json.dumps(sanitize(payload), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"{INFO} wrote {path.relative_to(Path.cwd())}")


def unwrap(result: Any) -> Any:
    """Pull the payload out of an MCP CallToolResult."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(text)
            return text
    return None


def line_items(cart: Any) -> list[dict[str, Any]]:
    """Best-effort extraction of cart lines; the real key is confirmed by this run."""
    if not isinstance(cart, dict):
        return []
    for key in ("products", "items", "cartProducts", "lines"):
        value = cart.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def fingerprint(cart: Any) -> list[tuple[str, Any]]:
    """A comparable summary of a cart, so 'unchanged' can be asserted."""
    out = []
    for item in line_items(cart):
        pid = item.get("productId") or item.get("product_id") or item.get("id")
        qty = item.get("quantity") or item.get("qty") or item.get("amount")
        out.append((str(pid), qty))
    return sorted(out)


async def serve_callback(bridge: AuthorizationBridge, port: int) -> asyncio.Task[None]:
    config = uvicorn.Config(create_app(bridge), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            return task
        await asyncio.sleep(0.05)
    raise RuntimeError("callback server did not start")


async def main(probe_cart: bool, port: int) -> int:
    base_url = os.environ.get("KOMORA_PUBLIC_BASE_URL", "http://localhost:8000")
    key = os.environ.get("KOMORA_TOKEN_ENCRYPTION_KEY")
    if not key:
        print(
            "KOMORA_TOKEN_ENCRYPTION_KEY is not set.\n"
            "Add it to backend/.env — generate one with:\n"
            '  uv run python -c "import base64,os;'
            'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
        )
        return 2

    server_url = os.environ.get("KOMORA_SILPO_MCP_URL", "https://mcp.silpo.ua/mcp")
    engine = make_engine(os.environ.get("KOMORA_DATABASE_URL", "sqlite+aiosqlite:///./verify.db"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)

    bridge = AuthorizationBridge()
    server_task = await serve_callback(bridge, port)

    async def show_url(telegram_id: int, url: str) -> None:
        print("\n" + "=" * 70)
        print("OPEN THIS IN A BROWSER AND SIGN IN TO SILPO:\n")
        print(url)
        print("=" * 70 + "\n")

    redirect_handler, callback_handler = bridge.handlers(LOCAL_USER, show_url)
    provider = PersistentOAuthClientProvider(
        server_url=server_url,
        client_metadata=build_client_metadata(base_url),
        storage=DBTokenStorage(
            telegram_id=LOCAL_USER,
            users=UserRepo(sessions),
            clients=OAuthClientRepo(sessions),
            cipher=TokenCipher(key),
        ),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    try:
        async with open_session(server_url, provider) as session:
            check("OAuth round-trip through our callback endpoint", True)

            # --- A2: capture the real tool schemas ---
            tools = await session.list_tools()
            declarations = [
                {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                for t in tools.tools
            ]
            check(f"list_tools returned {len(declarations)} tools", bool(declarations))
            dump("tools", declarations)

            names = {d["name"] for d in declarations}
            for expected in (
                "silpo_find_products_batch",
                "silpo_get_my_shopping_cart",
                "silpo_add_or_update_cart_products",
                "silpo_remove_cart_products",
            ):
                check(f"tool present: {expected}", expected in names)

            # --- reads ---
            found = unwrap(
                await session.call_tool(
                    "silpo_find_products_batch", {"queries": ["молоко", "хліб"]}
                )
            )
            check("find_products_batch returned data", found is not None)
            dump("find_products_batch", found)

            cart_id_raw = unwrap(await session.call_tool("silpo_get_my_shopping_cart", {}))
            cart_id = cart_id_raw if isinstance(cart_id_raw, str) else json.dumps(cart_id_raw)
            check("get_my_shopping_cart returned an id", bool(cart_id), f"cart id: {cart_id}")
            dump("my_shopping_cart_id", cart_id_raw)

            if isinstance(cart_id_raw, dict):
                cart_id = str(
                    cart_id_raw.get("id") or cart_id_raw.get("cartId") or cart_id_raw.get("guid")
                )

            before = unwrap(
                await session.call_tool("silpo_get_shopping_cart_by_id", {"cartId": cart_id})
            )
            dump("shopping_cart", before)
            before_print = fingerprint(before)
            print(f"{INFO} cart currently holds {len(before_print)} line(s)")

            if not probe_cart:
                print("\nRead-only run complete. Re-run with --probe-cart to verify A1.")
                return 0

            # --- A1: does add append, or replace? ---
            candidates = [
                p
                for p in (found if isinstance(found, list) else found.get("products", []))
                if isinstance(p, dict) and p.get("productId") and p.get("price")
            ]
            if not candidates:
                check(
                    "A1: found a product to test with", False, "no usable product in search results"
                )
                return 1
            item = min(candidates, key=lambda p: Decimal(str(p["price"])))
            payload = {
                "productId": item["productId"],
                "companyId": item.get("companyId"),
                "branchId": item.get("branchId"),
                "quantity": 1,
            }
            print(f"{INFO} probing with: {item.get('name')} ({item['price']})")

            async def read_cart() -> list[tuple[str, Any]]:
                return fingerprint(
                    unwrap(
                        await session.call_tool(
                            "silpo_get_shopping_cart_by_id", {"cartId": cart_id}
                        )
                    )
                )

            # Everything between the first add and the removal runs under try/finally:
            # a failed assertion mid-probe must not leave a stray item in a real cart.
            try:
                await session.call_tool(
                    "silpo_add_or_update_cart_products", {"cartId": cart_id, "products": [payload]}
                )
                after_add = await read_cart()
                added = dict(after_add).get(str(item["productId"]))

                check(
                    "A1a: adding did NOT wipe the existing cart",
                    all(entry in after_add for entry in before_print),
                    f"before={len(before_print)} after={len(after_add)}",
                )
                check("A1b: the item was added", added is not None, f"quantity={added}")

                # Add the same product again — upsert, or duplicate row?
                await session.call_tool(
                    "silpo_add_or_update_cart_products", {"cartId": cart_id, "products": [payload]}
                )
                after_second = await read_cart()
                occurrences = [pid for pid, _ in after_second if pid == str(item["productId"])]
                check(
                    "A1c: re-adding upserts rather than duplicating the line",
                    len(occurrences) == 1,
                    f"quantity now {dict(after_second).get(str(item['productId']))}, "
                    f"lines for this product: {len(occurrences)}",
                )
            finally:
                print(f"{INFO} restoring cart — removing the probe item")
                await session.call_tool(
                    "silpo_remove_cart_products",
                    {"cartId": cart_id, "productIds": [item["productId"]]},
                )
            restored = fingerprint(
                unwrap(
                    await session.call_tool("silpo_get_shopping_cart_by_id", {"cartId": cart_id})
                )
            )
            check(
                "A1d: cart restored to its original contents",
                restored == before_print,
                f"before={before_print}\n{INFO} after ={restored}",
            )
    finally:
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        await engine.dispose()

    failures = [label for status, label in _results if status == FAIL]
    print("\n" + "=" * 70)
    print(f"{len(_results) - len(failures)} passed, {len(failures)} failed")
    for label in failures:
        print(f"  FAILED: {label}")
    print("=" * 70)
    return 1 if failures else 0


if __name__ == "__main__":
    # Real environment variables win; .env fills the rest. Works identically in
    # PowerShell and bash, which shell-export instructions do not. Loaded before the
    # event loop starts rather than inside it.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-cart",
        action="store_true",
        help="verify A1 by adding and then removing one cheap item from your REAL cart",
    )
    parser.add_argument("--port", type=int, default=8000, help="local port for the callback")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.probe_cart, args.port)))
