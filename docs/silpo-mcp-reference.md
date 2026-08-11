# Silpo MCP — field reference and domain rules

Everything here was **verified against the live server** (2026-08-10/11) or read from
the schemas captured in `backend/tests/fixtures/mcp/tools.json`. Nothing is inferred
from tool names — four rounds of Task 7 went that way, and every guess was wrong.

Server: `https://mcp.silpo.ua/mcp` · 39 tools · streamable HTTP · OAuth 2.1 + PKCE.

> **Re-capture after any Silpo change:** `uv run python scripts/verify_mcp.py`
> (read-only). Add `--probe-cart` to re-verify the append semantics.

---

## 1. The call order is not optional

```
get_my_shopping_cart  ->  get_shopping_cart_by_id  ->  find_products_batch  ->  add_or_update
      shoppingCartId          branch + delivery ctx        products                cart
```

Product search **requires** `branchId`, `deliveryType`, `timeslotStart` and
`timeslotEnd`, and those only exist on the cart. This is why Silpo's own docs call
reading the cart *"always the first step"*. In Komora this is `SearchContext`
(`core/models.py`), extracted once and passed down.

## 2. Parameter names

| What you would guess | What Silpo wants |
|---|---|
| `cartId` | **`shoppingCartId`** |
| `queries` (search) | **`products`** |
| `productIds` (removal) | **`products`** |
| `products` (replacements) | **`productIds`** |
| `deliveryType` (time slots) | **`deliveryTypes`**, and an **array** |
| `couponId` / `id` (coupon details) | **`businessCouponId`**, and a **number** |

The last two are the same idea in both directions: the name that works for one tool is
the wrong one for its neighbour. Read the schema per tool, never per family.

### Which tools need the context

`branchId` is required by more than product search, and the set is not obvious:

| Tool | Needs |
|---|---|
| `find_products_batch`, `get_products`, `get_promotions`, `get_product_details` | branch + delivery type + **both** timeslot bounds |
| `get_replacements` | branch + delivery type + `companyId` — **no timeslot** |
| `get_categories` | branch only |
| `get_time_slots` | branch — but see §5.1, `start` is required in practice |
| `get_my_coupons`, `get_my_food_restrictions` | nothing — empty object |

`get_product_details` also insists the `slug` come from a search result: *"Never
construct from name."*

## 3. Response shapes

**`get_my_shopping_cart`** → `{"success": true, "shoppingCartId": "<uuid>"}`

**`get_shopping_cart_by_id`** → the cart is *nested*, and lines live under shipments:

```jsonc
{"success": true, "cart": {
  "id", "deliveryType", "timeslot": {"start", "end"}, "address",
  "shipments": [{"id", "companyId", "branchId", "products": [ /* lines */ ]}],
  "calculation": {"total", "totalAfterDiscounts", "subTotal", "subDiscount",
                  "productsTotal", "delivery", "payment", "validations": [...], "loyalty"},
  "checkoutWebLink", "checkoutMobileLink"   // present when the cart is checkout-ready
}}
```

**`find_products_batch`** → results are grouped *per query*, not flat:

```jsonc
{"success": true, "summary": "...", "queries": [
  {"query": "молоко", "totalFound": 5, "products": [ /* see below */ ]}]}
```

**`get_replacements`** → `{"items": [{"productId", "replacements": [ /* products */ ]}]}`.
Requires `branchId`, `companyId`, `productIds`, `deliveryType`.

### ⚠ The identifier trap

A **search result** names the product `id`. The **cart** names the same value
`productId`. Feeding a search result straight into `add_or_update_cart_products` fails
validation with an unhelpful message. Normalise at the boundary — Komora does it in
`passes/resolve.py`.

| Field | Search result | Cart line |
|---|---|---|
| identifier | `id` | `productId` |
| also present | `companyId`, `branchId`, `price`, `oldPrice`, `stock`, `available`, `weighted`, `step`, `slug`, `specialPrices`, `externalProductId` | `companyId`, `branchId`, `price`, `oldPrice`, `quantity`, `subTotal`, `subDiscount`, `total`, `stock`, `ratio`, `addToBasketStep` |

## 4. Cart write semantics — verified live

`add_or_update_cart_products` takes items with exactly these fields:

```jsonc
{"productId", "companyId", "branchId", "quantity"}   // all four required
{"addQuantity": bool, "comment": string}             // optional
```

**Send nothing else.** `name` and `price` are not declared. The schema does not set
`additionalProperties: false`, but nothing verifies that Silpo's validator agrees, and
this is the one call whose failure costs the user their basket.

Behaviour:

- **Appends.** Existing lines are untouched (3 → 5 lines observed; the user's three
  survived intact).
- **Sets quantity, does not increment.** Re-adding the same product with
  `quantity: 1` left it at 1, not 2. The schema explains why: `addQuantity` is
  *"Add to existing quantity (true) or replace (false)"*, and the verified default is
  replace. Komora leaves it off deliberately — summing would destroy the idempotency
  the retry path depends on.

Two consequences:

1. **A retried sync is idempotent by construction** — it cannot double-count. This is
   what makes the partial-failure retry path safe.
2. **Overlapping products do not sum.** If the user has 1 milk and you send 2, the
   result is 2. Never promise addition in a confirmation UI for a product already in
   the cart.

`clear_shopping_cart` exists and is never called by Komora except on an explicit
"start over" from the user.

## 4.1 Time slots — the parameter that is optional in the schema and required in life

`get_time_slots` returns:

```jsonc
{"success": true, "summary": "Found 25 time slots (25 available)",
 "slots": [{"start", "end", "available", "deliveryType", "deliveryCost",
            "deliveryCostMap", "minOrderCost", "maxWeight", "fast",
            "constraints": {"isLimitedAlcohol", "isLimitedTobacco",
                            "isLimitedCookedFood", "isLimitedOwnCooking"}}],
 "meta": {"total": 25}}
```

**`start` is optional in the schema and mandatory in practice.** Without it Silpo
answers with a window beginning at the **start of the current day** — so an evening
call gets back a list in which every slot has already passed and `available` is `false`
throughout. That reads exactly like "this branch has no delivery slots", and it is not.

Measured at 23:47 UTC on one branch:

| Call | Result |
|---|---|
| no `start`, `limit: 25` | 25 slots, **0 available**, window 06:00–18:30 *that same day* |
| no `start`, `limit: 100` | 78 slots, 52 available, spanning three days |
| `start` = tomorrow 00:00Z | 25 slots, **25 available** |

And the format is strict: a **timezone-qualified** ISO datetime works, while a
date alone (`2026-08-12`) or a naive one (`2026-08-12T00:00:00`) returns
**HTTP 500**. Komora passes the cart's own `timeslot.start`, which is always
offset-bearing because it came from Silpo.

`available` is the only field that decides anything — the list contains past slots, and
the tool's own description says *"Only pick slots where available=true."*

## 5. Domain rules Silpo states in its own tool descriptions

These are not suggestions — the descriptions read as a prescriptive agent playbook.

| Rule | Detail |
|---|---|
| **Show `totalAfterDiscounts`** | *"the actual amount the user will PAY"*. Never display `total`. |
| **Validate the timeslot** | Call `get_time_slots` **immediately** after reading the cart; if the cart's slot is not in the available set, make the user pick again before doing anything else. Times are UTC. |
| **Respect `stock`** | Never exceed it. Check first, cap, and tell the user the maximum. |
| **Never re-add plastic bags** | пакет / пакунок, when reordering from a cart. Genuinely non-obvious. |
| **Surface `validations[]`** | `level: "error"` entries **block checkout**; warnings must be communicated. **The `message` is a code, not prose** — see §5.1. |
| **Offer балабонуси** | If `calculation.loyalty` has `bonusAvailable > 0` and `isEnabled`, offer to apply them. |
| **Show both checkout links** | «Оформити на сайті» (`checkoutWebLink`) and «Оформити в застосунку» (`checkoutMobileLink`). |
| **Verify after writing** | Re-read the cart after `add_or_update` before telling the user anything. |

## 5.1 `calculation.validations[]` carries codes, not sentences

```jsonc
{"level": "error", "type": "timeslot",  "message": "timeslot.not_available", "context": []}
{"level": "error", "type": "product",   "message": "product.offer.stock.max", "context": [...]}
{"level": "info",  "type": "promotion", "message": "promotion.available",     "context": {...}}
```

`message` is a machine code. Rendering it verbatim puts «product.offer.stock.max» in
front of a Ukrainian-speaking shopper — which is exactly what the first live run did.
Translate the known ones and still show the unknown ones
(`bot/render.py: validation_text`): a checkout blocker nobody has written copy for yet
must not be hidden.

Codes observed so far: `timeslot.not_available`, `product.offer.stock.max`,
`promotion.available`. The same code can arrive more than once, one per offending line.

This is also the **authoritative** timeslot check. Silpo computes it against the real
cart, so it is worth more than any client-side comparison against `get_time_slots`.

## 6. What the API does *not* give you

This killed a headline feature, so it is worth stating plainly.

**Coupons cannot be matched to products.** And the two coupon endpoints differ in a way
that matters — an earlier version of this document ran them together and was wrong:

| | `get_my_coupons` | `get_coupon_details` |
|---|---|---|
| fields | `id, active, useWay, beginDate, endDate, description, limitText, warningText, image` | the same **plus** `state, usedCount, rewardText, rewardValue` |
| a discount value? | **never** — `additionalProperties: false` without one | yes, `rewardValue` |
| eligible products? | no | no |
| cost | one call | one call **per coupon** (`businessCouponId`) |

So the list endpoint alone cannot tell you what a coupon is worth. On the account this
was verified against, the only coupon's entire `description` was **«на онлайн чек»** —
a fragment. Its value, `−10%`, existed only in `get_coupon_details`. Komora therefore
enriches active coupons from the detail endpoint (capped, and degrading to the plain
coupon on failure).

Neither endpoint publishes an **eligible-product list**; the conditions are Ukrainian
prose in `limitText` — and real ones are multi-line bullets, so they cannot be dropped
into a one-line note. `get_promotions` is no better for arithmetic: `code`, `title`,
`productCount`, `url`, no amounts.

So *"swap brand X for Y and you trigger a 40% coupon"* **is not implementable**.
Inferring it from prose would be invention presented as arithmetic.

**What works instead, and is exact:** every product carries `price` and `oldPrice`,
with the discount already applied. `oldPrice − price` is a real, current, machine-
readable saving. Promotion `code`s can be passed to `get_products(promotionCode=…)` to
*discover* what is in a promotion.

Komora therefore reports savings that exist and shows coupons as text
(`passes/promos.py`), matching Silpo's own line: «Купони застосує Сільпо на касі».

### Envelopes, now captured

All three are `{"success", "summary", <payload key>}`:

| Tool | Payload key | Fixture |
|---|---|---|
| `get_my_coupons` | `coupons` | `my_coupons.json` |
| `get_my_food_restrictions` | `restrictions` | `my_food_restrictions.json` |
| `get_time_slots` | `slots` (**not** `timeslots`) + `meta.total` | `time_slots.json` |

`summary` is human prose worth reading while debugging — *"Found 25 time slots
(25 available)"*, *"No food restrictions set"*.

One caveat remains: the account these came from has **no food restrictions set**, so a
*populated* restrictions response has still never been seen. `core/pipeline.py:
_listed` keeps accepting several plausible shapes for that reason.

## 7. OAuth

```
/.well-known/oauth-protected-resource  ->  resource + authorization_servers
/.well-known/oauth-authorization-server ->  /authorize /token /register
```

- **DCR is open** — `POST /register` returns a `client_id` with no approval step.
- **A loopback redirect is accepted** (`http://localhost:8000/...` → 201), so local
  verification needs **no tunnel**. Register as `application_type: "native"` for
  loopback per RFC 8252; a deployed callback is `"web"`.
- `refresh_token` supported · PKCE **S256** · **no** `scopes_supported`, so send no scope.
- All endpoints sit at the origin, which dodges the pathful-AS bug in the `mcp` SDK.
- `client_secret_expires_at` exists — an expired DCR secret needs the shared
  registration row wiped so the next attempt re-registers.

See [verified external facts §2](superpowers/specs/2026-08-10-verified-external-facts.md)
for the `mcp` 2.0 SDK traps (renamed transport, httpx2, the open #3250 expiry bug).

## 8. Errors

A tool failure never raises and is never falsy. Three forms observed:

```
"MCP error -32602: Invalid arguments ..."                 protocol-level rejection
"Error in get-time-slots: API returned 500 Internal ..."  Silpo's own upstream failure
{"success": false, ...}                                   a structured refusal
```

The first cost a run of "8 passed, 0 failed" in which two calls had actually failed
validation. The second is worse, because it defeats the obvious fix: it carries no
`MCP error` prefix and no `success: false`, so a check for those passes it through as a
successful empty result. It was found by sending `get_time_slots` a naive datetime.

Hence the rule in `core/mcp/payload.py: error_of` — **any bare string is a failure.**
Every tool Komora calls declares an object output schema, so a string where an object
belongs is never a result.
