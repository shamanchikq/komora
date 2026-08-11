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

The last two are the same idea in both directions: the name that works for one tool is
the wrong one for its neighbour. Read the schema per tool, never per family.

### Which tools need the context

`branchId` is required by more than product search, and the set is not obvious:

| Tool | Needs |
|---|---|
| `find_products_batch`, `get_products`, `get_promotions`, `get_product_details` | branch + delivery type + **both** timeslot bounds |
| `get_replacements` | branch + delivery type + `companyId` — **no timeslot** |
| `get_time_slots`, `get_categories` | branch only |
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

## 5. Domain rules Silpo states in its own tool descriptions

These are not suggestions — the descriptions read as a prescriptive agent playbook.

| Rule | Detail |
|---|---|
| **Show `totalAfterDiscounts`** | *"the actual amount the user will PAY"*. Never display `total`. |
| **Validate the timeslot** | Call `get_time_slots` **immediately** after reading the cart; if the cart's slot is not in the available set, make the user pick again before doing anything else. Times are UTC. |
| **Respect `stock`** | Never exceed it. Check first, cap, and tell the user the maximum. |
| **Never re-add plastic bags** | пакет / пакунок, when reordering from a cart. Genuinely non-obvious. |
| **Surface `validations[]`** | `level: "error"` entries **block checkout**; warnings must be communicated. |
| **Offer балабонуси** | If `calculation.loyalty` has `bonusAvailable > 0` and `isEnabled`, offer to apply them. |
| **Show both checkout links** | «Оформити на сайті» (`checkoutWebLink`) and «Оформити в застосунку» (`checkoutMobileLink`). |
| **Verify after writing** | Re-read the cart after `add_or_update` before telling the user anything. |

## 6. What the API does *not* give you

This killed a headline feature, so it is worth stating plainly.

**Coupons cannot be matched to products.** `get_my_coupons` and `get_coupon_details`
return:

```
id, active, useWay, beginDate, endDate, description,
limitText, warningText, rewardText, rewardValue, image
```

A coupon has a *value* (`rewardValue`) but **no eligible-product list** — the conditions
are Ukrainian prose in `limitText`/`description`. `get_promotions` is no better for
arithmetic: `code`, `title`, `productCount`, `url`, no amounts.

So *"swap brand X for Y and you trigger a 40% coupon"* **is not implementable**.
Inferring it from prose would be invention presented as arithmetic.

**What works instead, and is exact:** every product carries `price` and `oldPrice`,
with the discount already applied. `oldPrice − price` is a real, current, machine-
readable saving. Promotion `code`s can be passed to `get_products(promotionCode=…)` to
*discover* what is in a promotion.

Komora therefore reports savings that exist and shows coupons as text
(`passes/promos.py`), matching Silpo's own line: «Купони застосує Сільпо на касі».

### Two response envelopes are still uncaptured

`get_my_coupons` and `get_my_food_restrictions` both returned **empty** on the account
verification ran against, so the shape of a populated response has never been observed
— only the field names, which come from the schemas. `core/pipeline.py: _listed`
therefore accepts the plausible envelopes (`{"coupons": […]}`, `{"items": […]}`, a bare
list) and treats anything else as empty rather than guessing one and crashing on the
others. **Re-capture these against an account that has coupons and restrictions set,
then replace the tolerance with the real shape.**

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

A tool failure arrives as an ordinary **string** beginning `MCP error -32602: ...`, or
as `{"success": false, ...}`. Neither raises. A truthiness check like
`if result:` reports failures as successes — that produced a run of
"8 passed, 0 failed" in which two calls had actually failed validation.

Always classify explicitly (`scripts/verify_mcp.py: error_of`).
