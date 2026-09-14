"""Plan 4 Task 0: re-capture the fixtures and close the questions the plan left open, live.

**Read-only.** Nothing here writes to the cart, favourites, delivery settings or promos.
It connects as a Telegram user already linked to the bot, exactly as
`capture_plan4.py` does — no registration, no login — and does three things:

1. **Fixtures.** `tools.json` (now with the MCP annotations the server added), and
   sanitised captures of the reads Plan 4 leans on: promotions, a promotion's products,
   one page of the discounted range, product sets, personal promos, coupons with their
   value fields, one coupon's details, one receipt with a non-empty `rewards[]`, and a
   `find_products_batch` hit carrying `displayRatio`/`displayPrice`/`specialPrices`.
   Family, profile, loyalty, addresses and certificates never become fixtures.
2. **Probes** (reference §10.8): 1 — promotion reads against a passed slot; 2 — `promoId`
   on receipt rewards; 3/4 — `specialPrices` types and coupon `progress`; 7 —
   `displayRatio`'s vocabulary and what `units.parse_display_ratio` cannot read;
   8 — whether an out-of-stock product disappears from article search; 12 — whether an
   online delivery also appears as a receipt.
3. **A report** on stdout and in `--out/summary.txt`: counts, shares and catalogue
   strings only. Account values — totals, dates, ids — never reach it.

Raw payloads are written to `--out`, which must be outside the repository.

    uv run python scripts/capture_task0.py --user <telegram_id> --out /tmp/task0
    uv run python scripts/capture_task0.py --user <telegram_id> --out /tmp/task0 --no-fixtures

Two sessions for one user can race a token refresh (reference §9). Run it while the bot
is idle for this user; a session refreshes only an expired token.
"""

import argparse
import asyncio
import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from _report import dump
from capture_plan4 import Capture, shape
from dotenv import load_dotenv

from komora.core.crypto import TokenCipher
from komora.core.habits.purchases import offline_purchases, online_purchases, receipt_totals
from komora.core.mcp.auth import (
    AuthorizationBridge,
    DBTokenStorage,
    PersistentOAuthClientProvider,
    build_client_metadata,
)
from komora.core.mcp.client import open_session
from komora.core.units import parse_display_ratio
from komora.db.base import make_engine, make_session_factory
from komora.db.repo import OAuthClientRepo, UserRepo

ROOT = Path(__file__).resolve().parent.parent
TOOL_KEYS = ("name", "description", "inputSchema", "outputSchema", "annotations")
OFFLINE_PAGE, ONLINE_PAGE = 10, 50
MAX_RECEIPT_PAGES = 40


def with_key(value: Any, key: str) -> list[dict[str, Any]]:
    """Every dict under `value` that carries `key` — shape-agnostic on purpose."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if key in value:
            found.append(value)
        for item in value.values():
            found.extend(with_key(item, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(with_key(item, key))
    return found


def shift_days(stamp: str, days: int) -> str:
    """The same wall-clock slot `days` earlier, keeping the server's own format."""
    shifted = date.fromisoformat(stamp[:10]) + timedelta(days=days)
    return shifted.isoformat() + stamp[10:]


def count_of(payload: Any, key: str) -> int:
    rows = (payload or {}).get(key) if isinstance(payload, dict) else None
    return len(rows) if isinstance(rows, list) else -1


def trimmed_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """One receipt, its line lists cut to three: a fixture needs the shape, not a basket."""
    return {
        key: (value[:3] if isinstance(value, list) and key != "rewards" else value)
        for key, value in receipt.items()
    }


async def tools_fixture(cap: Capture, write: bool) -> None:
    listed = await cap.session.list_tools()
    tools = []
    for tool in listed.tools:
        raw = tool.model_dump(by_alias=True, exclude_none=True)
        tools.append({key: raw[key] for key in TOOL_KEYS if key in raw})
    reads = [t["name"] for t in tools if (t.get("annotations") or {}).get("readOnlyHint")]
    cap.say(f"## tools/list: {len(tools)} tools, {len(reads)} with readOnlyHint")
    cap.say(f"annotated: {sum(1 for t in tools if t.get('annotations'))}/{len(tools)}")
    if write:
        dump("tools", tools, scrub=False)  # API definitions, no user data


async def context_of(cap: Capture) -> dict[str, Any] | None:
    cart_id = (await cap.call("silpo_get_my_shopping_cart", {}) or {}).get("shoppingCartId")
    cart = (
        await cap.call("silpo_get_shopping_cart_by_id", {"shoppingCartId": str(cart_id)})
        if cart_id
        else None
    )
    body = (cart or {}).get("cart", cart or {})
    shipments = body.get("shipments") or [{}]
    slot = body.get("timeslot") or {}
    ctx = {
        "branchId": shipments[0].get("branchId"),
        "deliveryType": body.get("deliveryType"),
        "timeslotStart": slot.get("start"),
        "timeslotEnd": slot.get("end"),
    }
    if not all(ctx.values()):
        cap.say("NO CART CONTEXT — pick a branch and a slot in the Silpo app, then rerun.")
        return None
    cap.say(f"context: deliveryType={ctx['deliveryType']} slot date {ctx['timeslotStart'][:10]}")
    return ctx


async def probe_stale_slot(cap: Capture, ctx: dict[str, Any]) -> None:
    """§10.8 item 1 — does a deal read survive a passed slot, as receipts do, or go
    empty, as search does? Decides whether the deal scan may leave the after-turn path."""
    cap.say("\n## Probe 1 — promotion reads against a passed slot")
    passed = {
        **ctx,
        "timeslotStart": shift_days(ctx["timeslotStart"], -3),
        "timeslotEnd": shift_days(ctx["timeslotEnd"], -3),
    }
    rows = []
    for label, context in (("live", ctx), ("passed", passed)):
        promos = await cap.call("silpo_get_promotions", context, f"p1_{label}")
        sale = await cap.call(
            "silpo_get_products",
            {**context, "mustHavePromotion": True, "inStock": True, "limit": 10},
            f"p1_{label}",
        )
        search = await cap.call(
            "silpo_find_products_batch", {**context, "products": ["молоко"]}, f"p1_{label}"
        )
        rows.append(
            (
                label,
                count_of(promos, "promotions"),
                count_of(sale, "products"),
                ((sale or {}).get("meta") or {}).get("total"),
                len(with_key(search, "externalProductId")),
            )
        )
    for label, promos_n, sale_n, total, hits in rows:
        cap.say(
            f"{label:>6}: promotions {promos_n} · mustHavePromotion page {sale_n}"
            f" (total {total}) · search hits {hits}   (-1 = the call failed)"
        )


async def fixtures_and_vocabulary(cap: Capture, ctx: dict[str, Any], write: bool) -> None:
    cap.say("\n## Deals fixtures, displayRatio vocabulary (probe 7), specialPrices (probe 3)")
    promos = await cap.call("silpo_get_promotions", ctx)
    promo_rows = (promos or {}).get("promotions", [])
    if write and promos:
        dump("promotions", promos)
    if promo_rows:
        smallest = min(promo_rows, key=lambda p: p.get("productCount") or 10**9)
        by_code = await cap.call(
            "silpo_get_products",
            {**ctx, "promotionCode": smallest["code"], "limit": 5},
            "by_promotion_code",
        )
        if write and by_code:
            dump("products_by_promotion", by_code)

    pages: list[dict[str, Any]] = []
    for offset in (0, 100, 200):
        page = await cap.call(
            "silpo_get_products",
            {**ctx, "mustHavePromotion": True, "inStock": True, "limit": 100, "offset": offset},
            f"on_sale_{offset}",
        )
        if not page:
            break
        if offset == 0 and write:
            dump("products_on_promotion", {**page, "products": page.get("products", [])[:20]})
        pages.extend(page.get("products", []))
        if len(page.get("products", [])) < 100:
            break

    ratios = [p.get("displayRatio") for p in pages]
    unit_forms = Counter(re.sub(r"[\d.,\s]+", "#", str(r)).strip() for r in ratios if r is not None)
    unreadable = Counter(str(r) for r in ratios if r is not None and parse_display_ratio(r) is None)
    weighted = [p for p in pages if p.get("weighted")]
    cap.say(
        f"{len(pages)} discounted products · displayRatio null {ratios.count(None)}"
        f" ({100 * ratios.count(None) / max(len(pages), 1):.1f} %)"
        f" · distinct strings {len(set(map(str, ratios)))}"
    )
    cap.say(f"unit forms: {dict(unit_forms.most_common())}")
    cap.say(f"parse_display_ratio cannot read: {dict(unreadable.most_common()) or 'none'}")
    weighted_ratios = dict(Counter(p.get("displayRatio") for p in weighted))
    cap.say(f"weighted {len(weighted)} · their ratios {weighted_ratios}")
    unit_mismatch = sum(
        1 for p in pages if not p.get("weighted") and p.get("displayPrice") != p.get("price")
    )
    cap.say(f"unit goods with displayPrice != price: {unit_mismatch}")
    special = [s for p in pages for s in (p.get("specialPrices") or [])]
    cap.say(
        f"specialPrices: {sum(1 for p in pages if p.get('specialPrices'))} products"
        f" · types {dict(Counter(s.get('type') for s in special))}"
    )

    # A search hit that carries every new field: the article of a multi-buy product when
    # there is one, so `specialPrices` is populated, plus an ordinary term.
    multi = next((p for p in pages if p.get("specialPrices") and p.get("externalProductId")), None)
    terms = [str(multi["externalProductId"])] if multi else []
    batch = await cap.call(
        "silpo_find_products_batch", {**ctx, "products": [*terms, "молоко"], "limit": 3}, "display"
    )
    hits = with_key(batch, "displayRatio")
    cap.say(
        f"find_products_batch display fixture: {len(hits)} hits with displayRatio,"
        f" article term {'present' if multi else 'absent'}"
    )
    if write and batch:
        dump("find_products_batch_display", batch)

    sets = await cap.call(
        "silpo_get_product_sets", {"branchId": ctx["branchId"], "deliveryType": ctx["deliveryType"]}
    )
    if write and sets:
        dump("product_sets", sets)


async def coupons_and_promos(cap: Capture, write: bool) -> set[str]:
    cap.say("\n## Coupons and personal promos (probe 4)")
    coupons = await cap.call("silpo_get_my_coupons", {})
    rows = (coupons or {}).get("coupons", [])
    if write and coupons:
        dump("my_coupons", coupons)
    new_fields = ("rewardText", "rewardValue", "rewardUnit", "rewardSign", "promoId", "endDateTime")
    cap.say(
        f"{len(rows)} coupons · active {sum(1 for c in rows if c.get('active'))}"
        f" · carrying {dict((f, sum(1 for c in rows if c.get(f) is not None)) for f in new_fields)}"
    )
    detailed: list[dict[str, Any]] = []
    for coupon in rows[:8]:
        detail = await cap.call(
            "silpo_get_coupon_details", {"businessCouponId": coupon["id"]}, "details"
        )
        if detail:
            detailed.append(detail)
    with_progress = [d for d in detailed if (d.get("coupon") or {}).get("progress") is not None]
    applicable = [(d.get("coupon") or {}).get("canBeAppliedToOrder") for d in detailed]
    active_not_applicable = sum(
        1
        for coupon, can in zip(rows, applicable, strict=False)
        if coupon.get("active") and can is False
    )
    cap.say(
        f"details read {len(detailed)} · progress non-null {len(with_progress)}"
        f" · canBeAppliedToOrder {dict(Counter(applicable))}"
        f" · active-but-not-applicable {active_not_applicable}"
    )
    for d in with_progress[:2]:
        progress = (d.get("coupon") or {}).get("progress")
        cap.say(f"  progress shape: {json.dumps(shape(progress), ensure_ascii=False)}")
    if write and detailed:
        dump("coupon_details", (with_progress or detailed)[0])

    promos = await cap.call("silpo_get_my_promos", {})
    if write and promos:
        dump("my_promos", promos)
    meta_keys = sorted((promos or {}).get("meta") or {})
    cap.say(f"personal promos: {count_of(promos, 'promos')} · meta keys {meta_keys}")
    ids = {str(c["promoId"]) for c in rows if c.get("promoId") is not None}
    return ids | {str(p["promoId"]) for p in (promos or {}).get("promos", []) if p.get("promoId")}


async def read_receipts(cap: Capture, ctx: dict[str, Any], since: str) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for page in range(MAX_RECEIPT_PAGES):
        payload = await cap.call(
            "silpo_get_my_offline_orders",
            {**ctx, "limit": OFFLINE_PAGE, "offset": page * OFFLINE_PAGE, "dateStart": since},
            f"page{page}",
        )
        orders = (payload or {}).get("orders", [])
        receipts.extend(orders)
        if len(orders) < OFFLINE_PAGE:
            return receipts
    cap.say(f"(receipts truncated at {MAX_RECEIPT_PAGES} pages)")
    return receipts


async def read_online(cap: Capture) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    for page in range(10):
        payload = await cap.call(
            "silpo_get_my_online_orders",
            {"limit": ONLINE_PAGE, "offset": page * ONLINE_PAGE},
            f"page{page}",
        )
        got = (payload or {}).get("orders", [])
        orders.extend(got)
        if len(got) < ONLINE_PAGE:
            break
    return orders


async def history_probes(
    cap: Capture, ctx: dict[str, Any], coupon_promo_ids: set[str], write: bool
) -> None:
    cap.say("\n## Probes 2 and 12 — receipts, rewards, and online orders")
    online = await read_online(cap)
    online_events = online_purchases({"orders": online})
    earliest = min((e.bought_at for e in online_events), default=None)
    since = (earliest.date() if earliest else date.today() - timedelta(days=365)).isoformat()
    receipts = await read_receipts(cap, ctx, f"{since}T00:00:00")
    offline_events = offline_purchases({"orders": receipts})
    totals = receipt_totals({"orders": receipts})

    rewards = [r for rec in receipts for r in (rec.get("rewards") or [])]
    with_id = [r for r in rewards if r.get("promoId") is not None]
    joined = [r for r in with_id if str(r.get("promoId")) in coupon_promo_ids]
    cap.say(
        f"receipts {len(receipts)} (asked from the first online order's date)"
        f" · with rewards {sum(1 for rec in receipts if rec.get('rewards'))}"
        f" · rewards {len(rewards)} · promoId non-null {len(with_id)}"
        f" · matching a current coupon's promoId {len(joined)}"
    )
    groups = Counter(r.get("rewardGroupCodeName") for r in rewards)
    cap.say(f"reward groups: {dict(groups.most_common(8))}")
    groups_with_id = Counter(r.get("rewardGroupCodeName") for r in with_id)
    cap.say(f"promoId non-null by group: {dict(groups_with_id)}")
    if write:
        sample = next((rec for rec in receipts if rec.get("rewards")), None)
        if sample:
            dump("my_offline_order_rewards", {"success": True, "orders": [trimmed_receipt(sample)]})

    # sumReg against the receipt's own line sums — Task 4 reads it as the receipt total.
    lines_by_receipt: dict[str, float] = defaultdict(float)
    for e in offline_events:
        lines_by_receipt[e.receipt_key] += float(e.unit_price) * e.qty
    close = sum(
        1
        for t in totals
        if t.receipt_key in lines_by_receipt
        and abs(float(t.total) - lines_by_receipt[t.receipt_key]) <= max(1.0, 0.02 * float(t.total))
    )
    cap.say(f"sumReg within 2 % of the line sums: {close}/{len(totals)}")

    # Item 12: an online order that also produced a receipt would share its products and
    # land within a few days of it. Overlap is measured on product keys, never on totals.
    receipt_days = sorted({e.bought_at.date() for e in offline_events})
    online_by_order: dict[str, list[Any]] = defaultdict(list)
    for e in online_events:
        online_by_order[e.receipt_key].append(e)
    offline_by_receipt: dict[str, list[Any]] = defaultdict(list)
    for e in offline_events:
        offline_by_receipt[e.receipt_key].append(e)
    span_start = receipt_days[0] if receipt_days else None
    comparable = matched = 0
    for events in online_by_order.values():
        day = events[0].bought_at.date()
        if span_start is None or day < span_start:
            continue
        comparable += 1
        keys = {e.product_key for e in events}
        for lines in offline_by_receipt.values():
            gap = (lines[0].bought_at.date() - day).days
            if 0 <= gap <= 3 and len(keys & {e.product_key for e in lines}) >= max(
                2, len(keys) // 2
            ):
                matched += 1
                break
    online_days = sorted({e.bought_at.date() for e in online_events})
    cap.say(
        f"online orders {len(online_by_order)} · receipts returned span"
        f" {'none' if not receipt_days else f'{(receipt_days[-1] - receipt_days[0]).days} days'}"
        f" · online orders inside that span {comparable}"
        f" · of those, with a receipt ≤ 3 days later sharing ≥ half its products {matched}"
    )
    if online_days and receipt_days:
        cap.say(
            f"last online order is {(receipt_days[0] - online_days[-1]).days} days"
            " before the first receipt returned (negative = the spans overlap)"
        )


async def probe_out_of_stock(cap: Capture, ctx: dict[str, Any]) -> None:
    """§10.8 item 8 — the description says an out-of-stock product may be missing from
    article search. Decides whether the deal scan reads a miss as unknown (default)."""
    cap.say("\n## Probe 8 — an out-of-stock product searched by article")
    gone: list[dict[str, Any]] = []
    # `get_products` refuses a browse with no category, promotion or set (400), so the
    # out-of-stock goods are looked for in the discounted range with the stock filter off
    # — from its tail, since the description sorts unavailable goods into the last group.
    args = {**ctx, "mustHavePromotion": True, "inStock": False, "limit": 100}
    head = await cap.call("silpo_get_products", {**args, "offset": 0}, "p8_head")
    total = int(((head or {}).get("meta") or {}).get("total") or 0)
    cap.say(f"discounted range with inStock=false: total {total}")

    def out_of_stock(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            p
            for p in rows
            if p.get("externalProductId")
            and (p.get("stock") in (0, 0.0) or p.get("available") is False)
        ]

    cap.say(
        f"out of stock on the first page: {len(out_of_stock((head or {}).get('products', [])))}"
    )
    groups: list[str] = []
    for offset in range(max(total - 300, 0), total, 100):
        page = await cap.call("silpo_get_products", {**args, "offset": offset}, f"p8_{offset}")
        rows = (page or {}).get("products", [])
        groups.extend("out" if out_of_stock([p]) else "in" for p in rows)
        gone.extend(out_of_stock(rows))
    if groups:
        first_out = groups.index("out") if "out" in groups else None
        cap.say(
            f"tail {len(groups)} products: out of stock {groups.count('out')}"
            f" · any in-stock after the first out-of-stock: "
            f"{first_out is not None and 'in' in groups[first_out:]}"
        )
    cap.say(f"out-of-stock products found in a catalogue browse: {len(gone)}")
    if not gone:
        cap.say("nothing out of stock surfaced — item 8 stays open")
        return
    articles = [str(p["externalProductId"]) for p in gone[:6]]
    batch = await cap.call(
        "silpo_find_products_batch", {**ctx, "products": articles, "limit": 3}, "p8"
    )
    found = {str(h.get("externalProductId")): h for h in with_key(batch, "externalProductId")}
    for p in gone[:6]:
        hit = found.get(str(p["externalProductId"]))
        verdict = (
            f"FOUND, stock={hit.get('stock')} available={hit.get('available')}"
            if hit
            else "MISSING from article search"
        )
        cap.say(
            f"  «{p.get('name')}» stock={p.get('stock')} available={p.get('available')} → {verdict}"
        )


async def restrictions(cap: Capture) -> None:
    cap.say("\n## Probe 5 — food restrictions (slugs only)")
    payload = await cap.call("silpo_get_my_food_restrictions", {})
    listed = [(r.get("slug"), r.get("name")) for r in (payload or {}).get("restrictions", [])]
    cap.say(f"restrictions: {listed}")


async def run(cap: Capture, write: bool) -> None:
    await tools_fixture(cap, write)
    ctx = await context_of(cap)
    await restrictions(cap)
    coupon_promo_ids = await coupons_and_promos(cap, write)
    if ctx is None:
        return
    await probe_stale_slot(cap, ctx)
    await fixtures_and_vocabulary(cap, ctx, write)
    await probe_out_of_stock(cap, ctx)
    await history_probes(cap, ctx, coupon_promo_ids, write)
    cap.say("\n## Errors")
    for err in cap.errors or ["none"]:
        cap.say(err)


async def main(user: int, out: Path, write: bool) -> int:
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
            try:
                await run(cap, write)
            finally:
                (out / "summary.txt").write_text("\n".join(cap.report), encoding="utf-8")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--user", type=int, required=True, help="a Telegram id linked to the bot")
    parser.add_argument("--out", type=Path, required=True, help="a directory OUTSIDE the repo")
    parser.add_argument(
        "--no-fixtures", action="store_true", help="probe and report only; write no fixture"
    )
    args = parser.parse_args()
    if args.out.resolve().is_relative_to(ROOT.parent):
        raise SystemExit("--out must be outside the repository: raw payloads hold personal data.")
    args.out.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(main(args.user, args.out, not args.no_fixtures)))
