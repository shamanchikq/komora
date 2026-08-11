"""Day-1 live verification against Silpo's MCP server. Run manually.

Proves the assumptions the whole sync design rests on, and captures real response
shapes as test fixtures.

  A1  `silpo_add_or_update_cart_products` APPENDS and upserts by product — it does
      not replace the cart. Inferred from tool naming; never actually observed.
  A2  Real tool JSON Schemas, captured so the Gemini converter (Task 8) can be
      tested against what Silpo actually sends rather than what we imagine.

Parameter names below come from the captured schemas, not from guesses. Learned the
hard way on the first run, when every one of them was wrong:
  shoppingCartId (not cartId) · products (not queries / productIds)
Product search also needs branch and delivery context, which only exists on the cart —
so the order is: get cart id -> read cart -> search -> mutate.

SAFETY. The A1 probe mutates your real Silpo cart, so it is opt-in via --probe-cart.
It records the cart first, adds two cheap items, then removes them and verifies the
cart matches the original. `silpo_clear_shopping_cart` is never called.

NO TUNNEL NEEDED. Silpo's Dynamic Client Registration accepts a loopback redirect
(verified 2026-08-10), and a loopback callback registers as a `native` client per
RFC 8252. A deployed Komora still needs a public HTTPS callback; this script does not.

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
# a probe item.
for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

PASS, FAIL, INFO = "PASS", "FAIL", "  ->"
_results: list[tuple[str, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    _results.append((PASS if ok else FAIL, label))
    print(f"[{PASS if ok else FAIL}] {label}" + (f"\n{INFO} {detail}" if detail else ""))
    return ok


def summarise() -> int:
    """Exit code from what actually happened. Every exit path goes through here so a
    failed check can never be reported as success."""
    failures = [label for status, label in _results if status == FAIL]
    print("\n" + "=" * 70)
    print(f"{len(_results) - len(failures)} passed, {len(failures)} failed")
    for label in failures:
        print(f"  FAILED: {label}")
    print("=" * 70)
    return 1 if failures else 0


def root_causes(exc: BaseException) -> list[BaseException]:
    """Flatten nested ExceptionGroups — the MCP transport nests task groups, so one
    failure otherwise surfaces as four stacked tracebacks."""
    if isinstance(exc, BaseExceptionGroup):
        return [cause for sub in exc.exceptions for cause in root_causes(sub)]
    return [exc]


def dump(name: str, payload: Any) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / f"{name}.json"
    path.write_text(
        json.dumps(sanitize(payload), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"{INFO} wrote {path.relative_to(Path.cwd())}")


def unwrap(result: Any) -> Any:
    """Pull the payload out of an MCP CallToolResult.

    Attributes are snake_case; the camelCase names are pydantic aliases only.
    """
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(text)
            return text
    return None


def error_of(payload: Any) -> str | None:
    """Return the error message if this payload is an MCP failure, else None.

    Essential: a validation error arrives as an ordinary string, so a bare
    `payload is not None` check reports failures as passes. The first run did exactly
    that — two calls "passed" while returning -32602 validation errors.
    """
    if isinstance(payload, str) and payload.lstrip().startswith("MCP error"):
        return payload.strip()
    if isinstance(payload, dict) and payload.get("success") is False:
        return json.dumps(payload, ensure_ascii=False)[:300]
    return None


def cart_context(cart: Any) -> dict[str, Any]:
    """Extract the branch and delivery context that product search requires.

    `silpo_find_products_batch` needs branchId, deliveryType, timeslotStart and
    timeslotEnd, and the schema says the branch comes from the cart — which is why
    Silpo's docs call reading the cart "always the first step".
    """
    if not isinstance(cart, dict):
        return {}
    flat: dict[str, Any] = {}

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 3 or not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in {"branchId", "deliveryType", "timeslotStart", "timeslotEnd"}:
                flat.setdefault(key, value)
            elif isinstance(value, dict):
                walk(value, depth + 1)

    walk(cart)
    return flat


def line_items(cart: Any) -> list[dict[str, Any]]:
    if not isinstance(cart, dict):
        return []
    for key in ("products", "items", "cartProducts", "lines"):
        value = cart.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def fingerprint(cart: Any) -> dict[str, Any]:
    """productId -> quantity, so 'unchanged' can be asserted."""
    out: dict[str, Any] = {}
    for item in line_items(cart):
        pid = item.get("productId") or item.get("product_id") or item.get("id")
        out[str(pid)] = item.get("quantity") or item.get("qty") or item.get("amount")
    return out


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
    config = uvicorn.Config(create_app(bridge), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)

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

            async def call(tool: str, args: dict[str, Any]) -> Any:
                return unwrap(await session.call_tool(tool, args))

            def call_ok(label: str, payload: Any) -> bool:
                problem = error_of(payload)
                return check(label, problem is None, problem or "")

            # --- A2: capture the real tool schemas ---
            tools = await session.list_tools()
            declarations = [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                    "outputSchema": t.output_schema,
                }
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

            # --- cart first: it carries the branch and delivery context search needs ---
            cart_id_raw = await call("silpo_get_my_shopping_cart", {})
            dump("my_shopping_cart_id", cart_id_raw)
            if not call_ok("silpo_get_my_shopping_cart", cart_id_raw):
                return summarise()

            cart_id = (
                cart_id_raw.get("shoppingCartId") if isinstance(cart_id_raw, dict) else cart_id_raw
            )
            if not check("cart id extracted", bool(cart_id), f"shoppingCartId={cart_id}"):
                return summarise()

            before_cart = await call("silpo_get_shopping_cart_by_id", {"shoppingCartId": cart_id})
            dump("shopping_cart", before_cart)
            if not call_ok("silpo_get_shopping_cart_by_id", before_cart):
                return summarise()

            before = fingerprint(before_cart)
            print(f"{INFO} cart currently holds {len(before)} line(s)")

            context = cart_context(before_cart)
            missing = [
                k
                for k in ("branchId", "deliveryType", "timeslotStart", "timeslotEnd")
                if k not in context
            ]
            if not check(
                "search context found on the cart",
                not missing,
                f"missing {missing}; have {list(context)}. See shopping_cart.json.",
            ):
                return summarise()

            found = await call(
                "silpo_find_products_batch",
                {**context, "products": ["молоко", "хліб"], "limit": 5},
            )
            dump("find_products_batch", found)
            if not call_ok("silpo_find_products_batch", found):
                return summarise()

            if not probe_cart:
                print("\nRead-only run complete. Re-run with --probe-cart to verify A1.")
                return summarise()

            # --- A1 ---
            pool = found if isinstance(found, list) else []
            if isinstance(found, dict):
                for value in found.values():
                    if isinstance(value, list):
                        pool.extend(v for v in value if isinstance(v, dict))
            candidates = [
                p for p in pool if isinstance(p, dict) and p.get("productId") and p.get("companyId")
            ]
            if not check(
                "A1: two products available to test with",
                len(candidates) >= 2,
                f"found {len(candidates)}; see find_products_batch.json",
            ):
                return summarise()

            first, second = candidates[0], candidates[1]

            def as_payload(product: dict[str, Any]) -> dict[str, Any]:
                return {
                    "productId": product["productId"],
                    "companyId": product["companyId"],
                    "branchId": product.get("branchId", context["branchId"]),
                    "quantity": 1,
                }

            print(f"{INFO} probing with: {first.get('name')} + {second.get('name')}")

            # Adding a second item proves append even when the cart started empty —
            # 'did it wipe the cart' is vacuous against an empty one.
            try:
                await call(
                    "silpo_add_or_update_cart_products",
                    {"shoppingCartId": cart_id, "products": [as_payload(first)]},
                )
                after_first = fingerprint(
                    await call("silpo_get_shopping_cart_by_id", {"shoppingCartId": cart_id})
                )
                check(
                    "A1a: first item added",
                    str(first["productId"]) in after_first,
                    f"quantity={after_first.get(str(first['productId']))}",
                )

                await call(
                    "silpo_add_or_update_cart_products",
                    {"shoppingCartId": cart_id, "products": [as_payload(second)]},
                )
                after_second = fingerprint(
                    await call("silpo_get_shopping_cart_by_id", {"shoppingCartId": cart_id})
                )
                check(
                    "A1b: adding APPENDS — the earlier item survived",
                    str(first["productId"]) in after_second
                    and str(second["productId"]) in after_second,
                    f"cart now holds {len(after_second)} line(s)",
                )
                check(
                    "A1c: pre-existing lines untouched",
                    all(pid in after_second for pid in before),
                    f"before={len(before)} now={len(after_second)}",
                )

                await call(
                    "silpo_add_or_update_cart_products",
                    {"shoppingCartId": cart_id, "products": [as_payload(second)]},
                )
                after_repeat = fingerprint(
                    await call("silpo_get_shopping_cart_by_id", {"shoppingCartId": cart_id})
                )
                check(
                    "A1d: re-adding upserts rather than duplicating",
                    len(after_repeat) == len(after_second),
                    f"quantity now {after_repeat.get(str(second['productId']))}, "
                    f"lines {len(after_repeat)} (was {len(after_second)})",
                )
            finally:
                print(f"{INFO} restoring cart — removing probe items")
                for product in (first, second):
                    if str(product["productId"]) not in before:
                        with contextlib.suppress(Exception):
                            await call(
                                "silpo_remove_cart_products",
                                {
                                    "shoppingCartId": cart_id,
                                    "products": [as_payload(product)],
                                },
                            )

            restored = fingerprint(
                await call("silpo_get_shopping_cart_by_id", {"shoppingCartId": cart_id})
            )
            check(
                "A1e: cart restored to its original contents",
                set(restored) == set(before),
                f"before={sorted(before)}\n{INFO} after ={sorted(restored)}",
            )
    except BaseException as exc:
        for cause in root_causes(exc):
            check(f"aborted: {type(cause).__name__}", False, str(cause)[:400])
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(server_task, timeout=5)
        await engine.dispose()

    return summarise()


if __name__ == "__main__":
    # Real environment variables win; .env fills the rest. Works identically in
    # PowerShell and bash. Loaded before the event loop starts rather than inside it.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-cart",
        action="store_true",
        help="verify A1 by adding and then removing two cheap items from your REAL cart",
    )
    parser.add_argument("--port", type=int, default=8000, help="local port for the callback")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.probe_cart, args.port)))
