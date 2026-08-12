"""End-to-end smoke test against the live Silpo server — everything except Telegram.

Task 14 is a manual checklist in a chat window, and most of what it is really testing
has nothing to do with Telegram: the OAuth gateway, the typed client's argument names,
the agent loop, the four passes, and the confirmation preview. All of that can be run
headlessly, which is what this does. What remains manual afterwards is the bot surface
itself — buttons, keyboards, message delivery.

It also captures the three response shapes that were still uncaptured after Task 13:
`get_my_coupons`, `get_my_food_restrictions` and `get_time_slots`. Each one currently
has tolerant, shape-guessing code behind it; a real fixture replaces a guess with a
fact.

SAFETY. Read-only by default: it stops at the preview, which is exactly the point the
bot stops at before the user's second tap. `--push` appends to your REAL cart and then
removes what it added, leaving anything that was already there untouched.
`silpo_clear_shopping_cart` is never called.

USAGE
    Configuration comes from backend/.env. A Telegram token is not needed — this
    script never touches Telegram.

        uv run python scripts/smoke_e2e.py
        uv run python scripts/smoke_e2e.py --push
        uv run python scripts/smoke_e2e.py --message "купи щось на сніданок"
        uv run python scripts/smoke_e2e.py --no-llm      # skip the model entirely
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import uvicorn
from _report import INFO, check, dump, note, skip, summarise
from dotenv import load_dotenv

from komora.api.app import create_app
from komora.bot.render import render_cart, render_sync_preview, render_sync_report
from komora.config import Settings
from komora.core.agent.loop import run_agent
from komora.core.agent.tools import build_tool_decls
from komora.core.crypto import TokenCipher
from komora.core.llm.factory import make_llm
from komora.core.mcp.auth import AuthorizationBridge
from komora.core.mcp.gateway import SilpoGateway
from komora.core.mcp.payload import root_causes
from komora.core.mcp.silpo import SilpoSession
from komora.core.models import DraftBasket, DraftLine
from komora.core.pipeline import (
    CartContextMissing,
    build_cart,
    load_context,
    timeslot_is_offered,
)
from komora.core.sync import execute_sync, preview_sync
from komora.db.base import Base, make_engine, make_session_factory
from komora.db.repo import OAuthClientRepo, UserRepo

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

LOCAL_USER = 1
"""The same operator id `verify_mcp.py` uses, so both scripts share one linked account."""

DEFAULT_MESSAGE = "купи молоко, хліб і щось до чаю"

FALLBACK_BASKET = DraftBasket(
    title="Звичайний кошик",
    intent="stated",
    lines=[
        DraftLine(description="молоко", quantity=1, reason_text="ви попросили"),
        DraftLine(description="хліб", quantity=1, reason_text="ви попросили"),
    ],
)
"""Used with --no-llm, so the Silpo half can be tested without a model at all."""


def banner(title: str) -> None:
    print(f"\n{'-' * 70}\n{title}\n{'-' * 70}")


async def capture_uncaptured(silpo: SilpoSession, context: Any) -> None:
    """Fixtures for the three shapes Komora currently guesses at.

    Each is wrapped separately: an account with no coupons still tells us the envelope,
    and one failing must not cost us the others.
    """
    for name, call in (
        ("my_coupons", silpo.get_my_coupons()),
        ("my_food_restrictions", silpo.get_my_food_restrictions()),
        (
            "time_slots",
            silpo.get_time_slots(
                branch_id=context.branch_id,
                delivery_type=context.delivery_type,
                start=context.timeslot_start,
            ),
        ),
    ):
        try:
            payload = await call
        except Exception as exc:
            check(f"capture {name}", False, f"{type(exc).__name__}: {exc}"[:300])
            continue
        check(f"capture {name}", True, f"keys: {sorted(payload)[:8]}")
        dump(name, payload)


async def validate_timeslot(silpo: SilpoSession, context: Any) -> None:
    """Silpo's own rule: confirm the cart's slot is still on offer.

    Uses `timeslot_is_offered` rather than its own logic, so the script cannot pass
    while the product disagrees.
    """
    verdict = await timeslot_is_offered(silpo, context)
    if verdict is None:
        skip("cart timeslot is still offered", "Silpo could not be asked")
        return
    check(
        "cart timeslot is still offered",
        verdict,
        f"cart slot {context.timeslot_start} — pick a new one in the Silpo app"
        if not verdict
        else f"cart slot {context.timeslot_start} is still available",
    )


async def main(args: argparse.Namespace) -> int:
    key = os.environ.get("KOMORA_TOKEN_ENCRYPTION_KEY")
    if not key:
        print("KOMORA_TOKEN_ENCRYPTION_KEY is not set — see backend/.env.example.")
        return 2

    settings = Settings(
        telegram_bot_token="unused-by-this-script",
        # Both tiers: the startup validator checks every tier, so leaving `full`
        # pointed at Gemini would demand an API key this script does not need.
        llm_lite=args.llm,
        llm_full=args.llm,
        _env_file=str(ENV_FILE),
    )
    check("settings load", True, f"llm={args.llm} · mcp={settings.silpo_mcp_url}")

    database_url = os.environ.get("KOMORA_SMOKE_DATABASE_URL", "sqlite+aiosqlite:///./verify.db")
    engine = make_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    users, clients = UserRepo(sessions), OAuthClientRepo(sessions)

    bridge = AuthorizationBridge()
    gateway = SilpoGateway(
        server_url=settings.silpo_mcp_url,
        public_base_url=settings.public_base_url,
        users=users,
        clients=clients,
        cipher=TokenCipher(key),
        bridge=bridge,
    )

    # Only needed if this run has to link an account; harmless otherwise.
    server = uvicorn.Server(
        uvicorn.Config(create_app(bridge), host="127.0.0.1", port=args.port, log_level="error")
    )
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

    try:
        if not await gateway.is_linked(LOCAL_USER):
            note("no stored tokens — running account linking first")
            await gateway.link(LOCAL_USER, show_url)
        check("account is linked", await gateway.is_linked(LOCAL_USER))

        async with gateway.connect(LOCAL_USER) as silpo:
            check("session opened with the stored token", True, "no re-login needed")

            banner("1. Tool declarations")
            tools = build_tool_decls(await silpo.list_tools())
            check(
                "declarations built from the live schemas",
                len(tools) > 1,
                f"{len(tools)} exposed: {[t.name for t in tools]}",
            )

            banner("2. Search context")
            try:
                cart_id, context = await load_context(silpo)
            except CartContextMissing as exc:
                check("load_context", False, f"{exc} — pick a store and slot in the Silpo app")
                return summarise()
            check(
                "load_context read branch and timeslot off the cart",
                True,
                f"branch={context.branch_id[:8]}… {context.delivery_type} "
                f"{context.timeslot_start} → {context.timeslot_end}",
            )

            banner("3. Shapes Komora still guesses at")
            await capture_uncaptured(silpo, context)
            await validate_timeslot(silpo, context)

            banner("4. Agent")
            if args.no_llm:
                skip("agent proposes a basket", "--no-llm: using a fixed draft")
                basket = FALLBACK_BASKET
            else:
                llm = make_llm(args.llm, settings)
                outcome = await run_agent(
                    llm=llm,
                    mcp=silpo,
                    context=context,
                    history=[],
                    user_message=args.message,
                    tools=tools,
                )
                if outcome.basket is None:
                    check(
                        "agent proposes a basket",
                        False,
                        f"answered with text instead: {outcome.reply!r}"[:300],
                    )
                    return summarise()
                basket = outcome.basket
                check(
                    "agent proposes a basket",
                    bool(basket.lines),
                    f"«{basket.title}» — {[ln.description for ln in basket.lines]}",
                )
                check(
                    "every proposed line carries a reason",
                    all(ln.reason_text.strip() for ln in basket.lines),
                )

            banner("5. Pipeline")
            cart = await build_cart(basket, silpo, context)
            check(
                "pipeline resolved the draft to real products",
                bool(cart.lines),
                f"{len(cart.lines)} line(s), total {cart.total} ₴, warnings {cart.warnings}",
            )
            check(
                "every resolved line has a product id and a reason",
                all(ln.product_id and ln.reason_text for ln in cart.lines),
            )
            print("\n" + render_cart(cart, basket.title))

            banner("6. Preview")
            preview = await preview_sync(cart, silpo)
            check(
                "preview reports the existing cart",
                True,
                f"existing={preview.existing_count} adding={preview.adding_count} "
                f"overlapping={preview.overlapping} drift={preview.drift}",
            )
            print("\n" + render_sync_preview(preview))

            if not args.push:
                banner("Stopping before the write")
                note("this is exactly where the bot stops before the user's second tap")
                note("re-run with --push to verify the append against your real cart")
                return summarise()

            banner("7. Append to the REAL cart")
            before = {
                str(p.get("productId"))
                for shipment in (
                    (await silpo.get_shopping_cart_by_id(cart_id)).get("cart") or {}
                ).get("shipments")
                or []
                for p in (shipment.get("products") or [])
            }
            report = await execute_sync(cart, silpo)
            print("\n" + render_sync_report(report))
            check("sync reported success", report.ok, f"failed: {report.failed}")
            if report.blocking_validations:
                # Silpo issues no link for a cart it will not check out — not a defect.
                skip("checkout link present", f"blocked by {report.blocking_validations}")
            else:
                check("checkout link present", bool(report.checkout_web_link))

            after_payload = await silpo.get_shopping_cart_by_id(cart_id)
            after = {
                str(p.get("productId"))
                for shipment in ((after_payload.get("cart") or {}).get("shipments") or [])
                for p in (shipment.get("products") or [])
            }
            check(
                "pre-existing lines untouched",
                before <= after,
                f"before={len(before)} after={len(after)}",
            )

            added = [ln for ln in cart.lines if not ln.unavailable]
            check(
                "every sent line is in the cart",
                all(ln.product_id in after for ln in added),
            )

            if args.keep:
                note("--keep: leaving the items in your cart")
                return summarise()

            banner("8. Restoring your cart")
            for line in added:
                if line.product_id not in before:
                    try:
                        await silpo.remove_cart_products(
                            cart_id,
                            [
                                {
                                    "productId": line.product_id,
                                    "companyId": line.company_id,
                                    "branchId": line.branch_id,
                                    "quantity": line.qty,
                                }
                            ],
                        )
                    except Exception as exc:
                        note(f"could not remove {line.name}: {exc}")

            restored_payload = await silpo.get_shopping_cart_by_id(cart_id)
            restored = {
                str(p.get("productId"))
                for shipment in ((restored_payload.get("cart") or {}).get("shipments") or [])
                for p in (shipment.get("products") or [])
            }
            check(
                "cart restored to its original contents",
                restored == before,
                f"before={sorted(before)}\n{INFO} after ={sorted(restored)}",
            )
    except BaseException as exc:
        for cause in root_causes(exc):
            check(f"aborted: {type(cause).__name__}", False, str(cause)[:400])
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5)
        except asyncio.CancelledError, TimeoutError:
            pass
        await engine.dispose()

    return summarise()


if __name__ == "__main__":
    load_dotenv(ENV_FILE)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", default=DEFAULT_MESSAGE, help="what to ask for")
    parser.add_argument(
        "--llm",
        default=os.environ.get("KOMORA_SMOKE_LLM", "ollama/gemma4:12b"),
        help="provider/model ref; local by default so no API key is needed",
    )
    parser.add_argument("--no-llm", action="store_true", help="skip the model entirely")
    parser.add_argument(
        "--push", action="store_true", help="append to your REAL cart, then remove it again"
    )
    parser.add_argument("--keep", action="store_true", help="with --push, do not clean up")
    parser.add_argument("--port", type=int, default=8000, help="local port for the callback")
    sys.exit(asyncio.run(main(parser.parse_args())))
