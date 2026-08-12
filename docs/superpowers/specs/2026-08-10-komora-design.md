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
| OAuth 2.1 + PKCE, dynamic client registration, tokens must be stored server-side | Backend + public HTTPS callback required regardless of surface. **Probed live 2026-08-10:** RFC 9728 metadata is published, `/register` (DCR) exists, `refresh_token` is supported, PKCE S256 available, all endpoints at the origin, no `scopes_supported`. See [verified external facts §1](2026-08-10-verified-external-facts.md). |
| `mcp` SDK 2.0 renamed/reshaped its OAuth + transport API, and ships an open blocker (#3250) that breaks silent token refresh | Task 6 must subclass `OAuthClientProvider` to restore `token_expiry_time`, keep DCR client info in **one shared row** (not per-user), persist an absolute `expires_at`, and set `application_type="web"`. See [§2](2026-08-10-verified-external-facts.md). |
| One persistent cart per user (`silpo_get_my_shopping_cart` — "always the first step") | We must read the existing cart and append; never replace. |
| `silpo_add_or_update_cart_products` upserts by `productId+companyId+branchId` | **CONFIRMED live 2026-08-11** — see §3.1. Appends; existing lines untouched. |
| `silpo_clear_shopping_cart` exists | Never called except on explicit user request ("почати заново"). |
| Rate limiting per user, 429 + backoff expected | Retry with exponential backoff; batch tools (`silpo_find_products_batch`, 30 items) preferred. |
| No recipe tools | LLM generates recipes; framed as a feature (unconstrained cuisine). |
| Offline receipts only captured when loyalty card was scanned | Habit data has gaps; confidence model must tolerate them. |
| Coupon application happens at Silpo checkout, not via MCP | All savings are estimates. UI copy: «Купони застосує Сільпо на касі… остаточна може відрізнятись». |

---

### 3.1 Verified against the live server — 2026-08-11

`scripts/verify_mcp.py --probe-cart` against a real account, **17/17 passed**. Fixtures
in `backend/tests/fixtures/mcp/`.

**A1 is confirmed.** Adding a second product left the first in place (cart 3 → 5 lines)
and the user's three pre-existing lines were untouched. The cart was then restored
exactly. Cart preparation as designed is safe.

**Refinement A1 did not anticipate:** re-adding the same product with `quantity: 1`
left the quantity at **1, not 2**. The call **sets** quantity, it does not increment.
Two consequences:

- **Sync is naturally idempotent** — a retried sync cannot double-count, which makes
  the partial-failure retry path in §10 safe by construction.
- **Overlapping products do not sum.** If the user already has 1 milk and Komora sends
  2, the result is 2, not 3. The confirm sheet must not promise addition for a product
  that is already in the cart.

**Field shapes — none of these matched what the tool names suggested:**

| | |
|---|---|
| Cart response | `{"success", "cart": {...}}`; lines at `cart.shipments[].products[]` |
| Search response | `{"success", "summary", "queries": [{"query", "totalFound", "products"}]}` |
| **Identifier trap** | search names it **`id`**; the cart names it **`productId`**. Normalise at the boundary. |
| Parameters | `shoppingCartId` (not `cartId`), `products` (not `queries`/`productIds`) |

**Product search is context-dependent.** `silpo_find_products_batch` requires
`branchId`, `deliveryType`, `timeslotStart`, `timeslotEnd` — and the branch comes from
the cart. The order is always: get cart id → read cart → search → mutate.

### Coupons cannot be matched to products — the savings maximizer needs rescoping

Discovered 2026-08-11 while building the passes, from the captured **output** schemas.

The spec claimed `silpo_get_my_coupons` + `silpo_get_coupon_details` "exposes which
products trigger each coupon". **They do not.** The real fields are:

```
id, active, useWay, beginDate, endDate, description,
limitText, warningText, rewardText, rewardValue, image
```

A coupon has a *value* (`rewardValue`) but **no eligible-product list** — the conditions
live in `limitText` and `description` as Ukrainian prose. `silpo_get_promotions` is no
better for arithmetic: `code`, `title`, `productCount`, `url`, and no amounts.

So the headline feature as written — *"swap brand X for Y and you trigger a 40% coupon"* —
**is not implementable.** Nothing in the API says which products trigger which coupon,
and inferring it from prose would be invention presented as arithmetic.

What the data does support, and it is better because it is exact:

- **Real savings from `oldPrice - price`.** Every product carries both, with the
  discount already applied. The cart fixture shows `price 39.99 / oldPrice 60.99`.
  That is a current, machine-readable saving needing no inference.
- **Promotion codes as a discovery filter** — pass `promotionCode` to
  `silpo_get_products` to list what is in a promotion. That powers the "deals" intent.
- **Coupons surfaced as text**, not applied. Which matches Silpo's own line:
  «Купони застосує Сільпо на касі».

Revised scope for feature 2: *report the savings already in your cart, and show the
coupons you hold* — not *optimize the cart toward coupons*. §7 pass 3 is renamed
accordingly and no longer proposes coupon-triggering swaps.

**Silpo's tool descriptions are a prescriptive playbook.** They specify behaviour our
design must adopt:

- **`cart.calculation.totalAfterDiscounts` is what the user actually pays** — Silpo says
  to always show this, never `total`. Our `ResolvedCart.total` must map to it, not to a
  sum of line prices.
- **Timeslot validation is mandatory**: call `silpo_get_time_slots` immediately after
  reading the cart and confirm the cart's slot is still available before anything else.
  Timeslot times are UTC; convert for display.
- **Never exceed `product.stock`** — check, cap, and tell the user the maximum.
- **Never re-add plastic bags** (пакет / пакунок) when reordering from a cart.
- **`cart.calculation.validations[]`** carries errors that *block checkout*, plus
  warnings. Both must be surfaced — this feeds §10's degraded-mode labelling.
- **Балабонуси**: if `cart.calculation.loyalty` shows `bonusAvailable > 0` and
  `isEnabled`, offering to apply them is expected behaviour.
- **`checkoutWebLink` / `checkoutMobileLink`** — show both, labelled «Оформити на
  сайті» and «Оформити в застосунку».

## 4. Architecture

```
silpo-chat/
├── backend/                  Python 3.12, uv
│   ├── core/                 pure domain. NO fastapi/aiogram/http imports
│   │   ├── mcp/              Silpo MCP client: typed wrappers, retry, cache
│   │   ├── agent/            LLM loop behind a provider-agnostic LLMClient
│   │                         protocol; Gemini impl v1 (see §4.1)
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

### 4.1 LLM provider (decided)

`core/llm/` defines an `LLMClient` protocol (`complete(messages, tools) -> Response`).
Providers are implementations behind a `make_llm("provider/model")` factory; switching
is config, not a refactor.

**Schema conversion is provider-scoped, not shared.** Gemini needs the lossy
OpenAPI-subset rewrite (`const`→`enum`, `$ref` inlining, `anyOf`-null→`nullable`);
Ollama accepts near-raw JSON Schema. Each provider owns its own conversion:

```
core/llm/
├── protocol.py          LLMClient, Message, ToolDecl, ToolCall, LLMResponse
├── factory.py           make_llm(ref) -> LLMClient
├── gemini/client.py     + gemini/schema_map.py  (the lossy rewrite)
└── ollama/client.py     JSON Schema passthrough, /api/chat
```

**Two roles, any provider.** *(Revised 2026-08-12. These were tiers named `lite` and
`full`, implying a weak/strong pair. They are jobs: `agent` writes the basket,
`verifier` checks it — and the verifier is deliberately the cheaper model, so a name
suggesting a ranking invited an "upgrade" that would make the pipeline worse.)*

Each role is bound to a model by a `provider/model` reference in config, so changing
model — or provider — is an env var, never a code change. **Point them at two different
models:** free-tier quota is keyed on (project, model) and a basket spends one request
on each role, so one model halves the baskets available per day.

```
KOMORA_LLM_AGENT=gemini/gemini-3.5-flash-lite      # production default
KOMORA_LLM_VERIFIER=gemini/gemini-3.1-flash-lite
# local development — free, offline, no API key:
KOMORA_LLM_AGENT=ollama/gemma4:12b
KOMORA_LLM_VERIFIER=ollama/gemma4:12b
```

Supported values (all verified 2026-08-10):

| Ref | Price /1M in-out | Notes |
|---|---|---|
| `gemini/gemini-2.5-flash-lite` | $0.10 / $0.40 | cost floor; no shutdown date |
| `gemini/gemini-3.1-flash-lite` | $0.25 / $1.50 | **default lite**; EOL 2027-05-07 |
| `gemini/gemini-3.5-flash-lite` | $0.30 / $2.50 | newest lite; speed/quality, not cheaper |
| `gemini/gemini-3.6-flash` | $1.50 / $7.50 | **default full** |
| `ollama/<any local tag>` | free | development, offline, and the privacy story |

**Ollama: development and privacy, explicitly NOT a production default.**

A live probe on 2026-08-10 (21 tool declarations, nested `propose_basket`, Ukrainian
prompt) showed `gemma4:12b` picking the right tool, filling the nested schema, and
writing Ukrainian reasons in 3.3 s; `gemma4:e4b` the same in 26 s.

**That probe proved less than it appeared to, and the initial reading of it here was
too generous.** Both of its pass criteria are metrics that pass unconditionally:

- *Single-call tool selection is saturated* — it does not distinguish a 4B local model
  from a frontier model. Komora's workload lives on the **multi-turn** axis, which the
  probe never exercised.
- *"Nested schema valid"* passes under any grammar-constrained backend regardless of
  model quality. The real failure mode is a **well-formed basket containing the wrong
  items** — silent, plausible, and worse than a hard error.
- Gemma is the most **prompt-format-fragile** family on BFCL; reformatting alone moves
  accuracy 34–67 points, so a two-prompt result is close to non-evidence.

On the axis that actually matters, the best measured local candidate (Gemma 4 26B-A4B)
scores **~45% BFCL multi-turn** against 61–68% for frontier hosted models. That number
is episode-level: roughly **one basket flow in two completes correctly**, before any
Ukrainian penalty (~25pp measured on comparable models for non-English tool selection)
and before quantization. For a shopping cart, the failure is a silently wrong order.

> **Gate:** no local model is promoted past `lite`, and never to `full`, without 50–100
> scored **Ukrainian multi-step episodes** measuring episode completion — not tool-pick
> accuracy. No such benchmark exists publicly; we would have to build it.

**Cost is not a valid reason to pursue local.** At ~$10/mo hosted, the engineering to
close a 16–23 point multi-turn gap (eval harness, template patching, likely fine-tuning)
costs many years of that bill. The defensible reasons are the **privacy/data-residency
story** (Ukrainian users' receipt histories never leaving the machine) and **free,
offline development**.

What the probe *does* legitimately support: the §4.1 read/write split is what makes any
local path arguable. The LLM only emits a `DraftBasket` of **descriptions**; resolving
SKUs, checking stock and optimizing coupons is deterministic Python. Tool count is also
a non-issue — collapse begins around 200 tools, far above our 21, so tool-RAG or
subsetting would be wasted effort.

Three integration facts that bite before model quality ever does:

1. **The Ollama/llama.cpp Gemma 4 chat template can silently drop tool-result messages**,
   producing an infinite re-call loop that looks like model stupidity and is actually an
   integration bug. Assert tool results round-trip into the next prompt before trusting
   any local multi-step result.
2. **`think: false` combined with `format` silently disabled schema constraints** on
   gemma4 (ollama#15260, fixed after 0.20.0). Require Ollama > 0.20.0 and validate every
   `propose_basket` payload regardless — malformed output is a retry, not a crash.
3. **Native `tools` only, never prompt-embedded JSON instructions.** Prompt-mode collapses
   multi-turn performance even for frontier models — worth more than three model
   generations.

Model notes: **drop `gemma4:e4b`** — at 5–8 sequential steps it is 2–3.5 minutes per user
turn, disqualifying for a chat bot irrespective of accuracy. `gemma4:12b` is ~16–26 s per
turn under the same load. Prefer **base instruct** models: `qwen3.6:27b` and a tuned
`gemma4-agent` both searched instead of proposing, which is not reasoning but the known
"Always-Call" pathology — and searching is the resolve pass's job. If a local production
default were ever genuinely required, the evidence points **away from Gemma** (xLAM-2-8B
reaches 70% multi-turn) — but those specialists have no Ukrainian, while the Ukrainian
specialists (MamayLM) sit on Gemma 2/3 bases scoring 5–11% multi-turn. **No open model
currently occupies both axes.** Plan for the hybrid, not for a single local model.

- ~~**Do not use Gemini 2.5 Flash-Lite** — it retires 16 Oct 2026.~~
  **CORRECTED 2026-08-10:** this was wrong, sourced from a third-party blog. The
  official deprecations table lists `gemini-2.5-flash-lite` as **"No shutdown date
  announced"**. It stays a legitimate cost-floor fallback ($0.10/$0.40 — 6× cheaper
  on output), one env var away. Conversely `gemini-3.1-flash-lite` *does* carry an
  announced EOL of **2027-05-07**. We keep it anyway (best price/quality point in
  the Gemini 3 family, ~9 months out), but model IDs must be env-overridable
  settings, never literals at call sites. See
  [verified external facts §3](2026-08-10-verified-external-facts.md).
- Model IDs are **bare** — no `-001` suffixes exist on Gemini 3.x.
- Gemini 3.5 Flash is superseded by 3.6 (same input, worse output price). Skip.
- DeepSeek V4 Flash is cheaper ($0.14/$0.28) but rejected: users' real receipt
  histories would leave for a jurisdiction we don't want to explain in a public
  README, and its peak/off-peak policy doubles prices unpredictably.
- **Use the `generateContent` API, not the newer Interactions API.** Interactions
  has no explicit caching and stores conversation history server-side by default
  (`store=True`) — a privacy decision we will not drift into with users' receipt data.
- **Never set `temperature`** on Gemini 3 (Google warns it risks looping/degradation;
  deprecated on 3.6). Set `thinking_level` explicitly — it defaults to `high`, which
  bills thinking tokens at the output rate.
- **Context caching: measure before optimizing.** Implicit caching needs a byte-stable
  prefix *and* a ~4096-token minimum our system prompt may not reach. Assemble the
  prompt cache-friendly from the start (stable content first), but verify hits via
  usage telemetry rather than assuming a 90% saving.
- Cost estimate: ~25k in / 2k out per cart session ≈ **1¢ (Flash-Lite) / 5¢
  (3.6 Flash)**. Mixed, ~20 carts/day ≈ **$10/mo**. AI Studio free tier covers
  most development.
- **Integration risk — now characterised, not speculative.** The SDK does *not*
  normalise hand-built function declarations, so the JSON Schema → Gemini converter
  is entirely ours and a bad schema simply 400s server-side. Exact rewrite rules
  (`anyOf`-null → `nullable`, `const` → `enum` and string-only, `$ref` inlining,
  `additionalProperties: true` rejected, `oneOf`/`allOf` fail server-side) are in
  [verified external facts §4](2026-08-10-verified-external-facts.md).

### 4.2 Hosting (decided)

**Hetzner CX22 (~€4/mo), Falkenstein or Nuremberg.** Docker Compose + Caddy
(automatic TLS). 2 vCPU / 4 GB; ~30–40 ms to Ukraine.

Deciding constraint: **the scheduler must run continuously.** Komora is
proactive — if the nightly habit recompute and deal scan don't fire, the core
feature is dead. This rules out every free tier that sleeps on idle (Render
free, anything scale-to-zero). Fly.io is an acceptable alternative if it never
scales to zero; Oracle always-free ARM is a fallback if capacity is obtainable.

**Development:** run the bot on **long polling** (aiogram supports it) — no
public URL needed for the bot at all. Only the OAuth callback needs a tunnel
(ngrok / Cloudflare Tunnel).

**Domain:** a `.app` domain (~$14/yr) — HSTS-preloaded, so HTTPS is forced,
which Telegram Mini Apps require regardless. Check `komora.app`; fallbacks
`mykomora.app`, `komora.food`. Free alternative: `.pp.ua` (Ukrainian
individuals), reads more hobbyist. Dynamic Client Registration means a changed
redirect URI just means re-registering — not a blocker to start, but pick
before M1.

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
3. **Savings reporting** — total the discounts already present in the resolved prices
   (`oldPrice − price`, exact and machine-readable) and surface the user's coupons as
   text. **Does not** propose coupon-triggering swaps: Silpo publishes no mapping from
   coupon to product, so such a swap would be invention. See §3.1.
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

**Resolved 2026-08-10:** hosting → Hetzner CX22 (§4.2); LLM → Gemini 3.1
Flash-Lite + 3.6 Flash behind an `LLMClient` protocol (§4.1); domain → buy a
`.app` before M1 (§4.2).

**Open:**

1. Exact domain — `komora.app` availability unchecked.
2. Hackathon registration (closes **31 Aug 2026**). Deferred by owner; M0–M3
   would make a competitive entry if taken up.
3. Postgres from the start vs SQLite-then-migrate. Spec assumes SQLite with a
   Postgres-compatible schema; revisit if multi-user load appears early.
