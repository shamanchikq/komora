# Komora (Комора) — Design Spec

**Date:** 2026-08-10
**Status:** Draft for review
**Product:** Personal grocery agent for Silpo customers — Telegram bot + Mini App, built on the official Silpo MCP.

---

## 1. Product identity

An agent that knows a household's shopping patterns and turns any shopping intent
into a real, in-stock, optimized Silpo cart. Habits are one input, not the thesis.

Five entry points, one pipeline, one destination:

```
INTENT                                DRAFT BASKET → PASSES → RESOLVED CART
"звичайний кошик"                                     │
"план на тиждень"                                     ├─ dietary/family filter
"вклади в 2000 ₴"                                     ├─ availability → substitutions
"що з акцій я реально куплю?"                         ├─ coupon/promo optimization
"вечірка на 10 у суботу"                              └─ budget fit
                                                      ↓
                                    confirmation → append to real Silpo cart
                                                      ↓
                                    checkout happens in Silpo (checkoutWebLink)
```

**Name:** Комора ("pantry"). Third-party product, adjacent to Silpo, never an
impersonation. Header carries «мініапп · не Сільпо». UI language: Ukrainian.

**Explicitly excluded:** fridge/receipt photo recognition (decided against),
native mobile apps, any checkout/payment handling.

---

## 2. Goals and non-goals

**Goals**

- G1: Any of the five intents produces a reviewed, validated cart appended to the
  user's real Silpo cart in ≤ 4 taps from a notification.
- G2: Every suggested item carries a visible, truthful reason.
- G3: Proactive nudges are rare, confident, and framed as receipt-history facts
  ("ви купуєте молоко кожні ~4 дні, минуло 6") — never fridge claims.
- G4: The savings number shown is an honest estimate with its uncertainty stated.
- G5: Public GitHub repo that reads as deliberate architecture (headless core,
  thin adapters).

**Non-goals**

- Payment, checkout, order placement (MCP does not expose it; we hand off).
- Multi-retailer support, price comparison with other chains.
- Social features, sharing carts between users (v1; see §12 household proposal).
- Web-first standalone identity — the PWA exists but Telegram is the only login.

---

## 3. Hard constraints (from the Silpo MCP)

| Constraint | Consequence |
|---|---|
| No order-placement tool; flow ends at cart + `checkoutWebLink`/`checkoutMobileLink` | We are a cart-preparation surface. Success state = handoff. |
| OAuth 2.1 + PKCE, dynamic client registration, tokens must be stored server-side | Backend + public HTTPS callback required regardless of surface. |
| One persistent cart per user (`silpo_get_my_shopping_cart` — "always the first step") | We must read the existing cart and append; never replace. |
| `silpo_add_or_update_cart_products` upserts by `productId+companyId+branchId` | Append semantics inferred from docs, **must be verified against live MCP on day 1**. |
| `silpo_clear_shopping_cart` exists | Never called except on explicit user request ("почати заново"). |
| Rate limiting per user, 429 + backoff expected | Retry with exponential backoff; batch tools (`silpo_find_products_batch`, 30 items) preferred. |
| No recipe tools | LLM generates recipes; framed as a feature (unconstrained cuisine). |
| Offline receipts only captured when loyalty card was scanned | Habit data has gaps; confidence model must tolerate them. |
| Coupon application happens at Silpo checkout, not via MCP | All savings are estimates. UI copy: «Купони застосує Сільпо на касі… остаточна може відрізнятись». |

---

## 4. Architecture

```
silpo-chat/
├── backend/                  Python 3.12, uv
│   ├── core/                 pure domain. NO fastapi/aiogram/http imports
│   │   ├── mcp/              Silpo MCP client: typed wrappers, retry, cache
│   │   ├── agent/            LLM loop (Anthropic SDK), conversation state
│   │   ├── intents/          usual · mealplan · budget · deals · event
│   │   ├── passes/           restrictions · availability · promos · budget
│   │   └── habits/           cadence + confidence engine
│   ├── api/                  FastAPI: /chat (SSE), /cart, /habits, OAuth callback,
│   │                         initData auth dependency
│   ├── bot/                  aiogram 3: thin adapter — conversation + push delivery
│   ├── jobs/                 APScheduler: nightly habit recompute, daily deal scan,
│   │                         notification dispatch
│   └── db/                   SQLAlchemy 2 + Alembic, SQLite → Postgres-ready
├── web/                      React + Vite + TS. Mini App (primary) + PWA (secondary)
│                             API types generated from FastAPI OpenAPI schema
└── docs/
```

**Boundary rule:** `core/` receives dependencies as interfaces (MCP client,
LLM client, repo/storage). The entire pipeline is testable without booting a
server, a bot, or the network.

**LLM division of labor (decided):**

- **Read tools — open to the LLM freeform.** Search, catalog, promotions,
  branches, time slots, profile, loyalty, order history. Arbitrary questions
  ("грузинські вина до 500 ₴?") work without a dedicated feature.
- **Write tools — pipeline only.** Cart mutations, favorites, delivery config go
  through deterministic, validated, confirmed code paths. The LLM never calls a
  write tool directly.

**Data flow:** intent handler (LLM-assisted) → `DraftBasket` (descriptions +
quantities, no SKUs) → passes (deterministic) → `ResolvedCart` (SKUs, prices,
coupons, substitution notes) → user confirmation → append to Silpo cart.

## 5. Data model

```
users              telegram_id (PK) · silpo_tokens_encrypted · branch_id ·
                   budget_weekly · quiet_hours · created_at
purchases          user_id · order_id · product_id · leaf_category_id · name ·
                   qty · unit_price · bought_at · source(online|offline)
                   UNIQUE(user_id, order_id, product_id)
product_habits     user_id · leaf_category_id · default_product_id ·
                   median_interval_days · cv · purchase_count · last_bought_at ·
                   confidence · muted
draft_baskets      id · user_id · title · intent · status(draft|confirmed|synced|
                   discarded) · created_at
draft_items        basket_id · product_id · name · qty · unit_price · reason_kind
                   (habit|deal|meal|sub|weak) · reason_text · substituted_from ·
                   optional · removed
notifications      user_id · kind · subject_key · sent_at
                   (dedupe: no repeat of (user_id,kind,subject_key) within cooldown)
conversations      user_id · role · content · created_at (rolling window)
price_snapshots    product_id · branch_id · price · captured_at   ← see §12.1
```

Encryption: Silpo tokens encrypted at rest (AES-GCM, key from env). SQLite for
v1; schema written to survive a Postgres move (no SQLite-only types).

## 6. Habits engine (decided rules)

- **Key:** leaf category from `silpo_get_categories_tree`, not productId (brand
  switching would fragment the signal) and not top-level category (too coarse).
  Most-frequent SKU within the group is stored as the default suggestion.
- Same-day purchases collapse into one event.
- **≥ 4 purchase events** required before the item can surface anywhere.
- Interval = **median** of gaps (bulk buys must not distort); expected next
  purchase scales with quantity bought.
- Confidence = f(purchase_count, coefficient of variation). Below threshold →
  **silence**, not hedged suggestions. Above → cadence shown with "~".
- Framing rule (product-wide): claims are about receipt history, never about
  the user's fridge. "Ви купуєте X кожні ~N днів, минуло M" — always true.
- Cold start: engine says nothing; the other four intents work on day one.
- User controls: mute per item (one tap, permanent until unmuted).

## 7. Passes (deterministic, ordered)

1. **Restrictions/family** — filter against `silpo_get_my_food_restrictions` and
   `silpo_get_my_family`; hard excludes, no LLM.
2. **Availability/resolution** — resolve descriptions to SKUs at the user's
   branch via `silpo_find_products_batch`; out-of-stock → `silpo_get_replacements`;
   substitution recorded with original, visible + reversible in UI.
3. **Promo/coupon optimization** — cross-reference `silpo_get_my_coupons` (+
   `silpo_get_coupon_details` eligible products), `silpo_get_my_promos`,
   `silpo_get_promotions`; may propose swaps ("бренд X → Y активує купон −40%");
   swaps are suggestions with reasons, never silent.
4. **Budget fit** — if cap set: compute headroom incl. current week's spend;
   over-cap → amber banner + one-tap "прибрати необов'язкові" (items marked
   `optional` by the intent handler). User may send over budget; their choice.

Nutrition scoring (from `silpo_get_product_details`) is a **read** on the
resolved cart, shown on request — not a pass that mutates it.

## 8. Surfaces

### Bot (aiogram)

- All conversation. Free-form chat → agent with read-tool access.
- Push: habit nudges, deal alerts, weekly digest. Quick replies: «Зібрати» /
  «Не зараз» / «Не стежити». Deep links open Mini App directly on target screen.
- Commands: /start (onboarding), /cart, /budget, /mute, /delete (full data wipe).

### Mini App (React)

Screens in build order: **Draft cart review** (designed, in claude.ai/design
project `a416d167`) → sync sheet (designed) → onboarding + learning payoff →
home → your usual → deals → meal plan.

Platform rules (decided):

- `WebApp.expand()` on launch; never `100vh` — use `viewportStableHeight`.
- `WebApp.disableVerticalSwipes()` — the cart is one long scroll.
- Native `MainButton` for the primary action; app's bottom element is the
  summary bar. Native `BackButton`.
- Komora owns its palette; only the light/dark signal is read from Telegram.
  Push colors outward via `setHeaderColor` / `setBackgroundColor` /
  `setBottomBarColor`.
- Desktop Telegram: centered max-width column, no hard-coded 390px.

### Design system (from the approved mockup)

- Fonts: Onest (UI) + IBM Plex Mono (numbers, codes).
- Light: ground #FCFAF6, card #FFFFFF (→ consider #FFFDF9 for the sheet),
  text #1E1A15, hint #8B8175, line #E7DFD3, sunken #F3EDE4.
- Dark: ground #17130F, card #201B16, text #F5EFE7.
- Brand: btn **#FF8522** with **#2A1200** text (7.28:1 AAA); wordmark #A85200
  light / #FF8522 dark. Brand appears on MainButton + wordmark only.
- Semantic: savings green ~oklch(0.50 0.10 152); substitution violet
  ~oklch(0.51 0.12 292); over-budget amber — **shift hue to 105–110 and
  dark-mode lightness to ~0.74** so it can't rhyme with brand orange.
- Trust copy is part of the design system: substitutions announced in a banner
  + struck-through original per row with «Повернути»; over-budget is the user's
  choice, not an error; thin data admits «усе тут — здогад»; footer states the
  draft lives in Komora until confirmed.

### Identity

Telegram is the only identity provider. Mini App sends `initData`; backend
verifies HMAC against bot token; maps to encrypted Silpo tokens. PWA outside
Telegram: "відкрийте через Telegram" for v1.

## 9. User journeys

**J1 — Recurring (the core loop).** Thursday 18:40 push: «Схоже, закінчуються:
молоко, хліб, кава. На каву купон −30%. Зібрати кошик?» → [Зібрати] → Mini App
opens on draft → remove 2 items, bump milk → MainButton → sheet: «У кошику
Сільпо вже 4 позиції — ми їх не чіпаємо. Додати 9?» → done → «Відкрити Сільпо».
Four taps.

**J2 — First contact.** /start → why-we-need-access, in plain words → OAuth in
browser → back in bot → «Аналізую ваші покупки за 6 місяців…» → learning payoff
screen (shopping rhythm, top items, «Відстежую 14 позицій») → first draft
offered. No history → skip payoff, offer meal plan / budget / deals instead.

**J3 — Weekly plan.** «Склади план на тиждень, бюджет 2500» → LLM builds menu
respecting restrictions + kids' ages → ingredient DraftBasket + usual staples →
passes → cart at ~2350 ₴ with 2 substitutions flagged → review → sync.

**J4 — Event.** «Шашлик на 10 людей у суботу» → quantity math → optional items
marked (dessert, wine) → over-budget banner offers trimming them first.

**J5 — Free-form question.** «Яке грузинське вино є до 500 ₴?» → agent calls
read tools directly, answers in chat. No cart involved.

**J6 — Deal alert (rare, high-precision).** Coupon lands on a high-confidence
habit item → one push with the concrete saving. Never for items below the
confidence threshold.

## 10. Error handling

**MCP layer**

- Retry with exponential backoff + jitter on 429/5xx (max 3); honor Retry-After.
- Circuit breaker per tool family; on open → degrade: build the cart without the
  failing pass and say so («не вдалося перевірити купони — суми без знижок»).
  Degraded results are labeled, never silent.
- Token refresh failure → bot message with re-auth link; draft preserved.

**Sync (the critical path)**

- Re-validate prices/availability immediately before append; if total drifts
  > 2% or any item went out of stock, show the delta in the confirm sheet
  before proceeding.
- Append item-by-item result tracking: partial failure → explicit list of what
  didn't make it («2 позиції не додались: …»), with retry. Never report success
  on partial sync. Draft stays until fully synced or discarded.
- Idempotency: sync operations carry a client-generated key; a retried sync must
  not double quantities (upsert semantics help; verify on day 1).

**LLM layer**

- Timeout → one retry → graceful fallback message. Tool-call loops capped
  (max steps + max tool calls per turn). Structured outputs validated
  (pydantic); invalid → one re-ask, then fallback.

**Jobs**

- All scheduled jobs idempotent; habit recompute is a full rebuild from
  `purchases` (no incremental state to corrupt). Notification dispatch checks
  the dedupe table inside the same transaction as the send record.

## 11. Testing

- **Unit (majority):** passes and habits are pure functions — fixture receipt
  histories in, expected carts/cadences out. Edge fixtures: bulk buys, brand
  alternation within a leaf category, receipt gaps, same-day splits, empty
  history. No network, no LLM.
- **MCP contract tests:** recorded request/response fixtures for every tool
  wrapper; a separate small **live smoke suite** (needs a real account, run
  manually) that asserts the day-1 assumptions — append semantics, upsert
  behavior, cart response shape.
- **Agent tests:** intent handlers tested with a stubbed LLM client returning
  canned tool-use sequences; assertion that write tools are *never* invoked
  by the agent path (guardrail test).
- **API:** FastAPI TestClient; `initData` HMAC verification (valid, tampered,
  expired); OAuth callback flow with a fake token server.
- **Web:** vitest component tests for cart math rendering (totals, savings,
  substitution states); Playwright happy-path later, not v1-blocking.
- **CI:** GitHub Actions — ruff, mypy, pytest, web typecheck + build.

## 12. Feature proposals (new — for review)

**12.1 Price memory (recommend: v1).** Nightly job snapshots prices of each
user's tracked items at their branch (`price_snapshots`). Unlocks honest
"ціна впала на 15% від звичайної" deal alerts — computed against *observed*
history, not marketing claims. Cheap: tens of products/user/day, batched.

**12.2 Expiring-coupon alert (recommend: v1).** `silpo_get_coupon_details`
includes terms; when a coupon matching a high-confidence habit item nears
expiry: «Купон −25% на вашу каву згорає в неділю». High precision, uses
existing dedupe machinery.

**12.3 Weekly digest (recommend: v1, off by default).** Sunday evening: spent
vs budget, saved via coupons, next week's likely needs. One message, opt-in.

**12.4 Delivery slot booking at sync (recommend: v1.1).**
`silpo_update_shopping_cart` supports slot/address; after append, offer «Слот
на завтра 18–20 вільний — забронювати?» Needs slot-availability UX care.

**12.5 Household sharing (backlog).** Multiple telegram_ids → one household:
shared habits, shared draft, "partner added items" ping. Real value, real
complexity (merge semantics, permissions) — after v1.

**12.6 Voice intents (backlog).** Telegram voice message → STT → intent. Cheap
to add later; pointless before the pipeline is solid.

**Rejected:** photo recognition (decided), standalone auth system (v1),
in-app recipes browser (LLM generates on demand; a browsing UI is a different
product).

## 13. Known limitations (user-facing, stated honestly)

- Checkout finishes in Silpo — Komora prepares, never pays.
- Savings are estimates; Silpo applies coupons at checkout.
- Habit learning needs ~4+ repeat purchases; in-store receipts only count when
  the loyalty card was scanned.
- Cadence claims are about purchase history, not fridge contents.
- One branch at a time; switching branch re-runs availability.
- MCP is hackathon-fresh: expect breaking changes; the typed client isolates them.

## 14. Rollout

- **M0 — Skeleton:** repo, CI, OAuth flow end-to-end, typed MCP client, day-1
  live verification of §3 assumptions (append/upsert semantics).
- **M1 — Pipeline:** DraftBasket→passes→ResolvedCart; bot-only flow with text
  cart; append + confirmation.
- **M2 — Mini App:** draft cart screen + sync sheet (from approved design),
  initData auth, deep links.
- **M3 — Habits:** import history, engine, nightly job, nudges + dedupe,
  learning-payoff onboarding.
- **M4 — Breadth:** meal plan, budget, deals, event intents; price memory;
  expiring-coupon alerts; weekly digest.
- **M5 — Polish:** remaining screens (your usual, deals, meal plan), PWA
  wrapper, README/architecture docs for the public repo.

Hackathon (optional): submission window closes **14 Sept 2026** — M0–M3 by then
is a competitive demo; decision deferred.

## 15. Open questions

1. Hosting for the public HTTPS callback + bot (Fly.io / Hetzner VPS / other?).
2. Anthropic API budget & default model (suggest claude-sonnet-5 for the loop,
   opus/fable only if meal-plan quality demands it).
3. Domain name for the Mini App + OAuth callback.
4. Whether to register for the hackathon (needed before 31 Aug).
