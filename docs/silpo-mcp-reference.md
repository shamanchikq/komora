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
| category `id` (product browse) | **the `slug`** — an id returns "No products found" |

The last two are the same idea in both directions: the name that works for one tool is
the wrong one for its neighbour. Read the schema per tool, never per family.

### How search behaves in Ukrainian

Measured against the live catalogue, because none of it is in the docs:

| | Result |
|---|---|
| **Inflection** | `молоко`→`молока` survives (shared `молок-` prefix); `яйця`→**`яєць` collapses to 4 hits**, the first an egg *container*. Prefix matching, not lemmatisation — and Ukrainian's stem-changing declensions defeat it. Ask the model for nominative forms. |
| **Script** | `кока кола`, `кока-кола`, `coca cola`, `Coca-Cola` all return the drink first. Silpo crosses Cyrillic↔Latin correctly; do not attempt it yourself. |
| **Word order** | `сир твердий` and `твердий сир` are identical. |
| **Precision** | A cliff, not a slope: `тісто для піци` returns exactly **1** product, `основа для піци` returns 7 of nonsense. |
| **Brands** | `яготинське`, `моршинська` work well on their own. |

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

### ⚠ `weighted` changes what `price` and `quantity` mean

| | `weighted: false` | `weighted: true` |
|---|---|---|
| `price` | per item | **per kilogram** |
| `quantity` | items | **kilograms** |
| `step` | 1 | the smallest orderable weight — 0.1 for cheese, 0.25 for sliced bacon |

So an unqualified `quantity: 1` on a weighted product orders **a whole kilo**. Live,
that put 2099 ₴ of 36-month Parmigiano into a carbonara basket — at a per-kilo price
that was entirely fair. Nothing in the response says "you probably meant 100 g";
`ratio` is `null` on every weighted product observed.

Komora resolves an unqualified quantity on a weighted good to one `step`
(`passes/resolve.py: clamp_quantity`), and an explicit amount is left alone.

### ⚠ Size is not in the search result — verified 2026-08-13

A search hit carries **no size, volume or weight field**, and the size is frequently not
in the name either. Three different Coca-Cola Zero products come back as the identical
string `"Напій Coca-Cola Zero"`, distinguishable only by price:

| name | price | how you tell them apart |
|---|---|---|
| Напій Coca-Cola Zero | 30,99 | you cannot, from the search |
| Напій Coca-Cola Zero | 34,49 | " |
| Напій Coca-Cola Zero | 56,49 | " |

`get_product_details(slug)` **does** carry it, under `attributes`:

```jsonc
"attributes": {
  "Розмір/об'єм": "<=0,5",       // the only size signal Silpo exposes
  "Торгова марка": "Coca-Cola", "Країна": "Україна", ...
}
```

Note the shape: `"<=0,5"` is a *bucket*, not a number, with a decimal comma. One call per
product, keyed by `slug`. Silpo is not the rate-limited resource here — the model is —
so reading it for a handful of candidates is affordable; reading it for a whole result
set is not.

Consequence: **a size qualifier cannot be honoured from search results alone.** «велика
кола зеро» has nothing to match on, and «велика кола зеро» as a *query* returns 0 hits
while «кола зеро» returns 6.

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

`remove_cart_products` takes a **different item** — read its schema, do not assume it
mirrors the add:

```jsonc
{"productId"}   // the only declared field, and the only required one
```

No `companyId`, no `branchId`, and no `quantity`: a removal takes the line out
entirely, so there is no amount to give. Sharing the add's four-field builder was
wrong twice over — it sent three undeclared fields to a *delete*, and it rejected any
caller that had no quantity to supply, which is every caller. (The A1 probe did send
all four and the server accepted them, so this is unverified surface rather than an
observed failure. Narrow it anyway: an accepted-today extra on a delete call is not a
guarantee worth holding.)

`clear_shopping_cart` exists and is never called by Komora except on an explicit
"start over" from the user. Removal of *named* products is separate and does happen —
Komora offers it only for lines it synced itself, and only behind the same second tap
that authorises an add.

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

## 4.2 A category is an aisle, not an answer

`get_products` has **no `query` parameter** — see its schema: the filters are
`category`, `mustHavePromotion`, `promotionCode`, `set`, price bounds and `sortBy`, and
at least one of the first four is required. It returns the category in Silpo's own
order with no idea what was asked for.

So a category browse cannot rank. Taking its first in-stock item is how «пармезан»
resolved to «Сир Мужон витриманий» — the cheese the user had just asked to replace —
three turns running, because that cheese sits at the top of the hard-cheese aisle.

**Use the category to filter search results, not to replace them.** The search supplies
relevance (Silpo's own, which beats anything hand-rolled — a word-overlap scorer was
tried and reverted for resolving «кока кола» to marmalade); the category supplies the
aisle. `resolve._narrow` intersects them by product `id`, since neither response
carries a category field.

`limit` on `get_products` caps at **100**. Ask for all of it: a shelf cut short at the
page limit cannot be told from a shelf the product is genuinely not on, and the two
want opposite fallbacks.

## 4.3 The category tree is the answer to "which of these did you mean"

`get_categories` returns `{id, parentId, slug, title}` and draws distinctions
free-text search cannot:

```
Яйця · Курячі яйця · Перепелині яйця · Фермерські яйця · Яйця інших птахів
```

Search for «яйця» and Silpo offers «Яйця цесарки» at 257,40 ₴ first — a perfectly good
string match. Browse `kuriachi-iaitsia-4977` and every result is an ordinary hen's egg.

```jsonc
get_products(category="kuriachi-iaitsia-4977", inStock=true, limit=5)
{"success": true, "summary": "Found 10 products (showing 5)",
 "products": [ /* same fields as a search hit: id, companyId, price, stock, … */ ],
 "meta": {"limit": 5, "offset": 0, "total": 10}}
```

**It is 1010 rows and `limit` caps at 1000 — paginate.** Asking for `limit=1000` and
stopping returns a suspiciously round, complete-looking answer that is missing ten
**top-level** categories, which orphans 71 of their children and makes «Вода»,
«Побутова хімія» and «Особиста гігієна» unmatchable. Follow `meta.total` with `offset`
(`passes/categories.py: fetch_categories`). A complete tree has **28 roots and no
orphans**, which is the cheap way to check a capture.

**`category` takes the `slug`.** Passing the `id` — which sits on the same object —
returns `"No products found"`, with no error and no hint. `inStock: true` is worth
setting: it cut 24 results to the 10 actually buyable.

Products come back in the **same shape as search results**, so nothing downstream needs
to know which call produced a candidate.

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

Codes observed so far — **only these; do not invent siblings**:

| Code | Level | Means |
|---|---|---|
| `timeslot.not_available` | error | the cart's delivery slot has lapsed |
| `product.offer.stock.max` | error | a line exceeds available stock |
| `order.cost.min` | error | the order is below the branch's minimum (`minOrderCost`) |
| `promotion.available` | info | a promotion the cart could qualify for; `context` names the products |

The naming is not guessable. An earlier version of `bot/render.py` carried a
hand-written `order.min_sum`, on the pattern of the others — the real code is
`order.cost.min`, and the invented one could never have fired. The same code can also
arrive more than once, one entry per offending line.

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

See verified external facts §2 (kept outside this repo)
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

## 9. Purchase history — the habits input, captured 2026-09-13

Three tools carry what a habits engine would need. All were called live against a
linked account; **all three answered `total: 0`**, so the shapes below are the *empty*
envelope and nothing more.

| Tool | Context required | `limit` | Observed |
|---|---|---|---|
| `silpo_get_my_online_orders` | none | 1–100, default 10 | `total: 0` — «No orders found» |
| `silpo_get_my_offline_orders` | `branchId`, `deliveryType`, `timeslotStart`, `timeslotEnd` — all four **required** | 1–**10**, default 10 | `total: 0` — «No offline orders found» |
| `silpo_get_my_favorites` | `branchId`, `deliveryType`, `timeslotStart` | 1–100 | `total: 0` — «No favorite products found» |

All three share one envelope, which is worth knowing because it is *not* the shape the
cart tools use:

```json
{ "success": true, "summary": "No orders found",
  "orders": [], "meta": { "limit": 10, "offset": 0, "total": 0 } }
```

`silpo_get_my_favorites` names its array `products`, not `orders`.

**The offline call is bound to a live cart.** Its four required parameters come from
`silpo_get_shopping_cart_by_id`, which means in-store purchase history cannot be read
without a branch and an unexpired timeslot — the same context a product search needs.
Any background job that imports history therefore depends on cart state, which is a
design constraint, not an implementation detail.

**`limit` really is capped at 10** for the offline tool (`"max: 10"` in its own schema),
against 100 for the online one. Sending 20 returns `-32602 too_big`. Read the schema.

### What is still unknown, and it is the important half

A populated response has **never been seen** — same standing as
`silpo_get_my_food_restrictions` (§6). The account it was called against holds a real
loyalty card (`typeName: "Постійна"`, status Active) and has simply never bought
anything, online or in store. So every question the habits engine's design depends on is
open:

- **Does an order line carry a category?** Spec §6 keys habits on the *leaf category*
  from `get_categories_tree`. If lines carry only `lagerId` and a name, the engine needs
  a resolution step per product, at a cost per import.
- **What timestamp does an order carry**, and at what granularity? The median-interval
  rule and the same-day collapse both need one.
- **How far back does paging reach** — `offset` has no documented ceiling, but 10 orders
  per call against an unknown history length sets the cost of a first import.
- **Which source is the real signal?** Most Silpo shopping is in store, so `offline` is
  probably where the ≥4 events come from — and that is the tool with the cart coupling
  and the smaller page.

What the tool descriptions claim, unverified: online orders come "with product details";
offline orders return `products[]` where `catalogProduct !== null` is reorderable and
`products[].lagerId` equals the `externalProductId` of the catalog tools, so a receipt
line can be matched to a product by searching that id numerically rather than by name.

**Plan 3 cannot be designed against this account.** It needs one linked to a person who
actually shops at Silpo; until then the line shape would be a guess, and every parameter
this project guessed from a tool name has turned out wrong.

### Populated online orders — a second account, 2026-09-13

An account that actually shops answered `silpo_get_my_online_orders` with
`total: 97`, ten per page. Types only; no value was printed or recorded
(`scripts/capture_history.py` prints the skeleton, not the payload):

```
orderId: str   number: str   status: str   createdAt: str   (ISO-8601 with offset)
amount: int    discount: float
address:  { city: str, street: str, building: str, apartment: str | null }
delivery: { type: str, deliveredAt: str, timeSlot: { from: str, to: str } }
products: [ { id: str, branchId: str, companyId: str, name: str, image: str,
              price: int, quantity: float, subtotal: float, removed: bool } ]
```

What that settles for the habits engine:

- **No category on a line, at any depth.** Spec §6 keys habits on the leaf category,
  so that key is one catalog lookup per *distinct* product, cached by product id — not
  something an import reads off the order. A household that rebuys the same things
  resolves far fewer products than it has lines.
- **Two dates, and they mean different things.** `createdAt` is when the order was
  placed; `delivery.deliveredAt` is when it arrived. "When did they last buy it" is the
  second — and an order that never arrived is a `status` to check, not a purchase.
- **`removed: bool` on a line.** A line can sit in an order without having been bought.
  An engine that counts it counts a purchase that did not happen.
- **A full online import is one call** for this account: the tool pages up to 100 and
  97 orders fit in one page. The capture asked for 10 at a time — the script's choice,
  not the API's. Receipts are the expensive side: 10 per call, and a live cart context.
- **`address` rides on every order.** `core/mcp/sanitize.py` redacts it by key, but an
  import has no reason to keep it at all.

Still open, because each needs a value and none was printed: whether `price` — an int
beside a float `subtotal` — is kopiykas; and whether `products[].id` is the catalog id
`get_products` returns. The tool description says the ids reorder through
`silpo_add_or_update_cart_products`, which implies yes; that is a claim, not a capture.

### Populated offline orders and favourites — same account, same day

Two runs first reported this account's cart as having no timeslot. It had one: the
capture script read the cart under `shoppingCart`, and the envelope is
`{"success": true, "cart": {...}}` — the key `pipeline._cart_body` already reads. The
lesson is the one at the top of this file, relearned inside the script written to obey it.

`silpo_get_my_offline_orders` answered `total: 21` — three calls for a full import:

```
createdAt: str   (like 9999-99-99T99:99:99 — no offset)
filId: int   filialName: str   cityName: str   receiptUrl: str
sumReg: int   sumDiscount: float   accruedBalaBonusesSum: float
chequeMagicName: str   chequePrediction: str
rewards:  [ { rewardGroupCodeName: str, applyText: str, valueText: str,
              applyRewardAmount: float, promoId: null } ]
products: [ { lagerId: int, name: str, price: float, quantity: int, unit: str, image: str,
              catalogProduct: { id: str, slug: str, branchId: str, companyId: str,
                                name: str, image: str, price: float, step: int,
                                stock: int, available: bool, weighted: bool } } ]
```

`silpo_get_my_favorites` answered `total: 20` (its default page is 25) in the `get_products`
format — `id: str` beside `externalProductId: int`, `slug`, `price`, `oldPrice`, `step`,
`stock`, `weighted` — and, being a set rather than events, carries no date.

What it settles:

- **No product payload in this API carries a category.** Not an online line, not a receipt
  line or its `catalogProduct`, not a favourite — and not the captured fixtures of
  `find_products_batch`, `get_products` or the cart, nor the *declared output schemas* of
  `find_products_batch`, `get_products`, `get_product_details` or
  `get_my_offline_orders`. The link runs one way only: `get_products` takes a category and
  returns its products. So spec §6's habits key — the leaf category — cannot be looked up
  per product. It must be derived by walking the tree and inverting the listings, or the
  key must change. That is a Plan 3 decision, to be made knowing this.
- **Receipt times have no timezone.** Online `createdAt` carries an offset; offline
  `createdAt` does not. Komora's timestamps are aware UTC and a naive one raises
  (`db/base.py`), so an import has to localise receipt times — to Europe/Kyiv, the only
  honest assumption about a Silpo till — before storing them.
- **A receipt line is `quantity: int` plus `unit: str`.** For a weighted good that is not
  obviously kilograms, and the line captured does not say. Spec §6's "expected next
  purchase scales with quantity bought" needs it settled against a weighted line.
- **`catalogProduct` can be null** (the tool description says so): a till item with no
  catalog counterpart has a `lagerId` and a name and nothing to key on.
- **`lagerId: int` and favourites' `externalProductId: int` agree in type**, which fits the
  description's claim that they are one identifier. Agreeing in type is not matching.
- **Receipts carry the store, the city and a `receiptUrl`.** An import needs none of them.
- **The capture's category check passed on `rewards.rewardGroupCodeName`** — a loyalty
  reward group — because `group` was among its hints. A false positive; the hints are
  narrowed to `categor`.

Still open: whether an online delivery *also* appears as a loyalty receipt — if it does, an
import reading both counts one purchase twice; the unit of a weighted receipt line; and
whether online `price: int` is kopiykas.
