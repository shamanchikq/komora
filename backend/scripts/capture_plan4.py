"""Plan 4 research capture: what the read tools Komora has never called return, live.

**Read-only.** Nothing here writes to the cart, favourites, delivery settings or promos.
Connects as a Telegram user already linked to the bot — the way `capture_history.py
--user` and `gateway.connect` do: no registration, no login — and asks every read tool
Plan 4 may lean on (deals, meal plans, events, digests, coupons, loyalty), once or
twice, following codes, slugs and ids from one tool into the next.

Per call it prints latency, payload size, a type shape and counts. Catalogue data gets
a short sample; **account data — profile, family, loyalty, certificates, addresses,
promo codes — gets key names and counts only**. Raw payloads are written to `--out`,
which must be outside the repository: they hold that account's personal data, and the
repository is public. Nothing is written to `tests/fixtures`.

It also diffs the server's live tool list against `tests/fixtures/mcp/tools.json`,
captured 2026-08-11: the server is young, and a plan written against a month-old
schema is a plan written against a guess.

    uv run python scripts/capture_plan4.py --user <telegram_id> --out /tmp/plan4

Two sessions for one user can race a token refresh (reference §9, Plan 3 Task 3). Run
it while the bot is idle for this user; a session refreshes only an expired token.
"""

import argparse
import asyncio
import json
import os
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from komora.core.crypto import TokenCipher
from komora.core.mcp.auth import (
    AuthorizationBridge,
    DBTokenStorage,
    PersistentOAuthClientProvider,
    build_client_metadata,
)
from komora.core.mcp.client import call_tool, open_session
from komora.core.mcp.payload import error_of, unwrap
from komora.core.mcp.sanitize import sanitize
from komora.db.base import make_engine, make_session_factory
from komora.db.repo import OAuthClientRepo, UserRepo

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_TOOLS = ROOT / "tests" / "fixtures" / "mcp" / "tools.json"

PERSONAL = frozenset(
    {
        "silpo_get_my_profile",
        "silpo_get_my_family",
        "silpo_get_loyalty_info",
        "silpo_get_my_certificates",
        "silpo_get_my_delivery_addresses",
        "silpo_get_promo_codes",
    }
)
"""Tools whose values never reach stdout — shapes and counts only."""


def shape(value: Any, depth: int = 0, limit: int = 6) -> Any:
    """The structure of a payload, with lists summarised as `[n × item-shape]`."""
    if depth > limit:
        return "…"
    if isinstance(value, dict):
        return {k: shape(v, depth + 1, limit) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return "[0]"
        kinds = {json.dumps(shape(v, depth + 1, limit), sort_keys=True) for v in value[:20]}
        first = shape(value[0], depth + 1, limit)
        return {f"[{len(value)} ×{' (varies)' if len(kinds) > 1 else ''}]": first}
    if value is None:
        return "null"
    return type(value).__name__


class Capture:
    def __init__(self, session: Any, out: Path) -> None:
        self.session = session
        self.out = out
        self.timings: dict[str, list[float]] = {}
        self.errors: list[str] = []
        self.report: list[str] = []

    def say(self, text: str = "") -> None:
        print(text)
        self.report.append(text)

    async def call(self, tool: str, args: dict[str, Any], label: str = "") -> Any:
        name = f"{tool}{'__' + label if label else ''}"
        started = time.perf_counter()
        try:
            payload = unwrap(await call_tool(self.session, tool, args))
        except Exception as exc:  # a research script records every failure and goes on
            elapsed = time.perf_counter() - started
            self.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            self.say(f"\n### {name}  ✗ {type(exc).__name__} after {elapsed:.2f}s: {exc}")
            return None
        elapsed = time.perf_counter() - started
        self.timings.setdefault(tool, []).append(elapsed)
        (self.out / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        size = len(json.dumps(payload, ensure_ascii=False))
        problem = error_of(payload)
        self.say(
            f"\n### {name}  {elapsed:.2f}s · {size:,} chars"
            + (f" · ✗ {problem}" if problem else "")
        )
        shown_args = {k: v for k, v in args.items() if k not in ("timeslotStart", "timeslotEnd")}
        self.say(f"args: {json.dumps(shown_args, ensure_ascii=False)[:200]}")
        if problem:
            self.errors.append(f"{name}: {problem[:200]}")
            return None
        self.say("shape: " + json.dumps(shape(payload), ensure_ascii=False)[:1500])
        if isinstance(payload, dict) and payload.get("summary"):
            self.say(f"summary: {payload['summary']}")
        return payload

    def sample(self, tool: str, rows: list[Any], fields: tuple[str, ...], n: int = 5) -> None:
        if tool in PERSONAL:
            return
        for row in rows[:n]:
            if isinstance(row, dict):
                picked = {f: sanitize(row.get(f)) for f in fields if f in row}
                self.say("  · " + json.dumps(picked, ensure_ascii=False)[:300])


async def live_tool_diff(cap: Capture) -> None:
    listed = await cap.session.list_tools()
    live = {t.name: t.model_dump(by_alias=True, exclude_none=True) for t in listed.tools}
    fixture_raw = json.loads(FIXTURE_TOOLS.read_text(encoding="utf-8"))
    old_list = (
        fixture_raw.get("tools", fixture_raw) if isinstance(fixture_raw, dict) else fixture_raw
    )
    old = {t["name"]: t for t in old_list}
    (cap.out / "tools_live.json").write_text(
        json.dumps(list(live.values()), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    cap.say(f"\n## Live tool list: {len(live)} tools (fixture 2026-08-11: {len(old)})")
    cap.say(f"next cursor: {getattr(listed, 'next_cursor', None)!r}")
    added, removed = sorted(set(live) - set(old)), sorted(set(old) - set(live))
    cap.say(f"added: {added or 'none'}")
    cap.say(f"removed: {removed or 'none'}")
    for name in sorted(set(live) & set(old)):
        a, b = old[name], live[name]
        changes = []
        for key in ("inputSchema", "outputSchema"):
            pa = set((a.get(key) or {}).get("properties", {}))
            pb = set((b.get(key) or {}).get("properties", {}))
            ra = set((a.get(key) or {}).get("required", []))
            rb = set((b.get(key) or {}).get("required", []))
            if pa != pb:
                changes.append(f"{key} props +{sorted(pb - pa)} -{sorted(pa - pb)}")
            if ra != rb:
                changes.append(f"{key} required +{sorted(rb - ra)} -{sorted(ra - rb)}")
            if (
                json.dumps(a.get(key), sort_keys=True) != json.dumps(b.get(key), sort_keys=True)
                and pa == pb
            ):
                changes.append(f"{key} changed inside (same top-level props)")
        if a.get("description") != b.get("description"):
            changes.append(
                f"description {len(a.get('description', ''))}→{len(b.get('description', ''))} chars"
            )
        if b.get("annotations"):
            changes.append(f"annotations {b['annotations']}")
        if changes:
            cap.say(f"changed {name}: " + "; ".join(changes))


async def server_facts(cap: Capture) -> None:
    cap.say("\n## Server")
    getter = getattr(cap.session, "get_server_capabilities", None)
    caps = getter() if callable(getter) else None
    if caps is not None:
        cap.say(
            "capabilities: " + json.dumps(caps.model_dump(exclude_none=True), ensure_ascii=False)
        )
    for probe in ("list_resources", "list_prompts", "list_resource_templates"):
        method = getattr(cap.session, probe, None)
        if method is None:
            cap.say(f"{probe}: not on this SDK")
            continue
        try:
            result = await method()
            items = next((v for k, v in result.model_dump().items() if isinstance(v, list)), [])
            cap.say(f"{probe}: {len(items)}")
        except Exception as exc:
            cap.say(f"{probe}: refused — {type(exc).__name__}: {str(exc)[:120]}")


def discount_stats(cap: Capture, products: list[dict[str, Any]], label: str) -> None:
    with_old = [p for p in products if p.get("oldPrice")]
    pct = [
        round(100 * (p["oldPrice"] - p["price"]) / p["oldPrice"], 1)
        for p in with_old
        if p.get("oldPrice") and p.get("price") is not None and p["oldPrice"] > 0
    ]
    special = [p["specialPrices"] for p in products if p.get("specialPrices")]
    types = Counter(s.get("type") for sp in special for s in sp)
    cap.say(
        f"{label}: {len(products)} products · {len(with_old)} with oldPrice"
        + (
            f" · discount % min {min(pct)} median {statistics.median(pct)} max {max(pct)}"
            if pct
            else ""
        )
        + f" · {len(special)} with specialPrices {dict(types)}"
        + f" · weighted {sum(1 for p in products if p.get('weighted'))}"
        + f" · unavailable {sum(1 for p in products if not p.get('available'))}"
    )
    for sp in special[:3]:
        cap.say(f"  specialPrices sample: {sp}")


async def run(cap: Capture) -> None:
    await server_facts(cap)
    await live_tool_diff(cap)

    cap.say("\n## Cart context")
    cart_id = (await cap.call("silpo_get_my_shopping_cart", {}) or {}).get("shoppingCartId")
    cart = (
        await cap.call("silpo_get_shopping_cart_by_id", {"shoppingCartId": str(cart_id)})
        if cart_id
        else None
    )
    body = (cart or {}).get("cart", cart or {})
    shipments = body.get("shipments") or [{}]
    slot = body.get("timeslot") or {}
    branch, delivery = shipments[0].get("branchId"), body.get("deliveryType")
    cap.say(
        f"deliveryType={delivery}"
        f" slot={slot.get('start')}→{slot.get('end')}"
        f" branch={str(branch)[:8]}…"
    )
    calc = body.get("calculation") or {}
    cap.say("calculation.loyalty: " + json.dumps(shape(calc.get("loyalty")), ensure_ascii=False))
    cap.say("calculation keys: " + ", ".join(sorted(calc)))
    if not (branch and delivery and slot.get("start") and slot.get("end")):
        cap.say("NO CART CONTEXT — pick a branch and a slot in the Silpo app, then rerun.")
        return
    ctx = {
        "branchId": branch,
        "deliveryType": delivery,
        "timeslotStart": slot["start"],
        "timeslotEnd": slot["end"],
    }
    slots = await cap.call(
        "silpo_get_time_slots",
        {"branchId": branch, "start": slot["start"], "limit": 5},
        "from_cart_slot",
    )
    live = [s for s in (slots or {}).get("slots", []) if s.get("start") == slot["start"]]
    cap.say(f"cart slot offered: {bool(live and live[0].get('available'))}")
    for s in (slots or {}).get("slots", [])[:2]:
        cap.say(
            f"  slot: minOrderCost={s.get('minOrderCost')}"
            f" deliveryCost={s.get('deliveryCost')}"
            f" costMap={s.get('deliveryCostMap')}"
        )

    cap.say("\n## Deals")
    promos = await cap.call("silpo_get_promotions", ctx)
    promo_rows = (promos or {}).get("promotions", [])
    counts = [p.get("productCount") or 0 for p in promo_rows]
    if counts:
        cap.say(
            f"{len(promo_rows)} promotions"
            f" · productCount min {min(counts)}"
            f" median {statistics.median(counts)}"
            f" max {max(counts)} total {sum(counts)}"
        )
    cap.sample("silpo_get_promotions", promo_rows, ("code", "title", "productCount", "url"), n=8)
    if promo_rows:
        biggest = max(promo_rows, key=lambda p: p.get("productCount") or 0)
        by_code = await cap.call(
            "silpo_get_products",
            {**ctx, "promotionCode": biggest["code"], "limit": 20},
            "by_promotion_code",
        )
        rows = (by_code or {}).get("products", [])
        discount_stats(cap, rows, f"promotion {biggest.get('title')!r}")
        cap.sample(
            "silpo_get_products",
            rows,
            ("name", "price", "oldPrice", "specialPrices", "weighted", "stock"),
        )
        cap.say(f"meta: {(by_code or {}).get('meta')}")
    on_sale = await cap.call(
        "silpo_get_products",
        {**ctx, "mustHavePromotion": True, "inStock": True, "limit": 100},
        "must_have_promotion",
    )
    sale_rows = (on_sale or {}).get("products", [])
    discount_stats(cap, sale_rows, "mustHavePromotion (in stock)")
    cap.say(f"meta: {(on_sale or {}).get('meta')}")
    sorted_sale = await cap.call(
        "silpo_get_products",
        {
            **ctx,
            "mustHavePromotion": True,
            "sortBy": "promotion",
            "sortDirection": "desc",
            "limit": 10,
        },
        "sorted_by_promotion",
    )
    cap.sample(
        "silpo_get_products", (sorted_sale or {}).get("products", []), ("name", "price", "oldPrice")
    )

    cap.say("\n## Sets, categories, similar, details")
    sets = await cap.call("silpo_get_product_sets", {"branchId": branch, "deliveryType": delivery})
    set_rows = (sets or {}).get("sets", [])
    cap.sample("silpo_get_product_sets", set_rows, ("slug", "title", "description"), n=12)
    if set_rows:
        in_set = await cap.call(
            "silpo_get_products", {**ctx, "set": set_rows[0]["slug"], "limit": 10}, "by_set"
        )
        cap.sample(
            "silpo_get_products", (in_set or {}).get("products", []), ("name", "price", "oldPrice")
        )
        cap.say(f"meta: {(in_set or {}).get('meta')}")
    popular = await cap.call(
        "silpo_get_popular_categories", {"branchId": branch, "deliveryType": delivery}
    )
    pop_rows = (popular or {}).get("categories", [])
    cap.sample("silpo_get_popular_categories", pop_rows, ("slug", "title"), n=10)
    if pop_rows:
        slug = pop_rows[0]["slug"]
        await cap.call(
            "silpo_get_category",
            {"branchId": branch, "deliveryType": delivery, "categorySlug": slug},
        )
        priced = await cap.call(
            "silpo_get_products",
            {
                **ctx,
                "category": slug,
                "inStock": True,
                "sortBy": "price",
                "sortDirection": "asc",
                "fromPrice": 20,
                "toPrice": 60,
                "limit": 10,
            },
            "category_price_band",
        )
        prices = [p.get("price") for p in (priced or {}).get("products", [])]
        cap.say(
            f"price band 20–60 ₴ sorted asc: {prices}"
            f" (inside band: {all(20 <= (x or 0) <= 60 for x in prices)})"
        )
    pick = next((p for p in sale_rows if p.get("slug")), None)
    if pick:
        details = await cap.call("silpo_get_product_details", {**ctx, "slug": pick["slug"]})
        product = (details or {}).get("product") or {}
        attrs = product.get("attributes") or {}
        cap.say(f"details for {pick.get('name')!r}: attribute keys {list(attrs)[:40]}")
        cap.say(
            "  attributes sample: "
            + json.dumps(dict(list(attrs.items())[:12]), ensure_ascii=False)[:700]
        )
        cap.say(f"  ratio={product.get('ratio')!r} images={len(product.get('images') or [])}")
        similar = await cap.call(
            "silpo_get_similar_products",
            {"branchId": branch, "slug": pick["slug"], "limit": 8, "deliveryType": delivery},
        )
        cap.sample(
            "silpo_get_similar_products",
            (similar or {}).get("products", []),
            ("name", "price", "oldPrice"),
        )
    food = next((p for p in sale_rows if p.get("slug") and p.get("weighted")), None)
    if food and food is not pick:
        weighted_details = await cap.call(
            "silpo_get_product_details", {**ctx, "slug": food["slug"]}, "weighted"
        )
        attrs = ((weighted_details or {}).get("product") or {}).get("attributes") or {}
        cap.say(f"weighted {food.get('name')!r}: attribute keys {list(attrs)[:40]}")

    cap.say("\n## Coupons, promos, loyalty")
    coupons = await cap.call("silpo_get_my_coupons", {})
    coupon_rows = (coupons or {}).get("coupons", [])
    cap.say(
        f"{len(coupon_rows)} coupons"
        f" · active {sum(1 for c in coupon_rows if c.get('active'))}"
        f" · useWay {dict(Counter(c.get('useWay') for c in coupon_rows))}"
    )
    cap.sample(
        "silpo_get_my_coupons",
        coupon_rows,
        ("id", "active", "useWay", "beginDate", "endDate", "description", "limitText"),
        n=6,
    )
    for coupon in coupon_rows[:4]:
        detail = await cap.call(
            "silpo_get_coupon_details", {"businessCouponId": coupon["id"]}, str(coupon["id"])
        )
        c = (detail or {}).get("coupon") or {}
        cap.say(
            f"  coupon {coupon['id']}: state={c.get('state')}"
            f" usedCount={c.get('usedCount')}"
            f" rewardText={c.get('rewardText')!r}"
            f" rewardValue={c.get('rewardValue')!r}"
            f" endDate={c.get('endDate')}"
        )
        cap.say(f"    limitText: {str(c.get('limitText'))[:300]!r}")
    my_promos = await cap.call("silpo_get_my_promos", {})
    promo_list = (my_promos or {}).get("promos", [])
    cap.say(
        f"{len(promo_list)} personal promos"
        f" · selected {sum(1 for p in promo_list if p.get('selected'))}"
        f" · meta {(my_promos or {}).get('meta')}"
    )
    cap.sample(
        "silpo_get_my_promos",
        promo_list,
        ("selected", "endDate", "description", "rewardText", "rewardValue", "limitText"),
        n=6,
    )
    codes = await cap.call("silpo_get_promo_codes", {})
    cap.say(f"promo codes: {len((codes or {}).get('promoCodes', []))}")
    loyalty = await cap.call("silpo_get_loyalty_info", {})
    balance = ((loyalty or {}).get("loyalty") or {}).get("balance") or {}
    cap.say(
        f"loyalty: card present {bool(((loyalty or {}).get('loyalty') or {}).get('card'))}"
        f" · balance currency {balance.get('currency')!r}"
        f" · account types {[a.get('type') for a in balance.get('accounts', [])]}"
    )
    premium = await cap.call("silpo_get_my_premium_subscription", {})
    cap.say(
        f"premium: status {(premium or {}).get('status')!r}"
        f" · features {[f.get('name') for f in (premium or {}).get('features') or []][:8]}"
    )

    cap.say("\n## Household and preferences (counts only)")
    family = await cap.call("silpo_get_my_family", {})
    kids = (family or {}).get("children", [])
    cap.say(
        f"family: members {len((family or {}).get('members', []))} · children {len(kids)}"
        f" (with dateOfBirth {sum(1 for k in kids if k.get('dateOfBirth'))})"
        f" · pets {len((family or {}).get('pets', []))}"
    )
    restrictions = await cap.call("silpo_get_my_food_restrictions", {})
    cap.say(
        f"food restrictions: "
        f"{[r.get('slug') for r in (restrictions or {}).get('restrictions', [])]}"
    )
    profile = await cap.call("silpo_get_my_profile", {})
    cap.say(
        f"profile keys filled: "
        f"{[k for k, v in ((profile or {}).get('profile') or {}).items() if v]}"
    )
    favourites = await cap.call(
        "silpo_get_my_favorites",
        {"branchId": branch, "deliveryType": delivery, "timeslotStart": slot["start"], "limit": 5},
    )
    cap.say(f"favourites meta: {(favourites or {}).get('meta')}")
    certificates = await cap.call("silpo_get_my_certificates", {"limit": 5})
    cap.say(f"certificates: {len((certificates or {}).get('certificates', []))}")
    addresses = await cap.call("silpo_get_my_delivery_addresses", {})
    cap.say(f"delivery addresses: {len((addresses or {}).get('addresses', []))}")
    branches = await cap.call("silpo_list_branches", {"limit": 3})
    cap.say(f"branches meta: {(branches or {}).get('meta')}")
    tree = await cap.call("silpo_get_categories_tree", ctx)
    cap.say(f"categories tree top-level keys: {list((tree or {}).keys())[:6]}")

    cap.say("\n## Latency (s) per tool")
    for tool, samples in sorted(cap.timings.items(), key=lambda kv: -max(kv[1])):
        cap.say(
            f"{tool:<42} n={len(samples)}"
            f" max {max(samples):.2f}"
            f" median {statistics.median(samples):.2f}"
        )
    cap.say("\n## Errors")
    for err in cap.errors or ["none"]:
        cap.say(err)


async def main(user: int, out: Path) -> int:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("KOMORA_TOKEN_ENCRYPTION_KEY")
    if not key:
        print("KOMORA_TOKEN_ENCRYPTION_KEY is not set — see backend/.env.example.")
        return 2

    server_url = os.environ.get("KOMORA_SILPO_MCP_URL", "https://mcp.silpo.ua/mcp")
    base_url = os.environ.get("KOMORA_PUBLIC_BASE_URL", "http://localhost:8000")
    engine = make_engine(os.environ.get("KOMORA_DATABASE_URL", "sqlite+aiosqlite:///./verify.db"))
    sessions = make_session_factory(engine)

    async def refuse(_: int, __: str) -> None:
        raise RuntimeError(f"user {user} holds no usable Silpo tokens; this script never logs in")

    bridge = AuthorizationBridge()
    redirect_handler, callback_handler = bridge.handlers(user, refuse)
    provider = PersistentOAuthClientProvider(
        server_url=server_url,
        client_metadata=build_client_metadata(base_url),
        storage=DBTokenStorage(
            telegram_id=user,
            users=UserRepo(sessions),
            clients=OAuthClientRepo(sessions),
            cipher=TokenCipher(key),
            redirect_uri=None,
        ),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    try:
        async with open_session(server_url, provider) as session:
            cap = Capture(session, out)
            await run(cap)
            (out / "summary.txt").write_text("\n".join(cap.report), encoding="utf-8")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--user", type=int, required=True, help="a Telegram id already linked to the bot"
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="a directory OUTSIDE the repository"
    )
    args = parser.parse_args()
    if args.out.resolve().is_relative_to(ROOT.parent):
        # Checked before the event loop starts: raw payloads hold personal data.
        raise SystemExit("--out must be outside the repository: raw payloads hold personal data.")
    args.out.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(main(args.user, args.out)))
