# Prompt for Claude Design — correct `Komora Draft Cart.dc.html`

Written 2026-08-18, after Plan 1 shipped and its manual checklist passed 8/8 against
live Silpo. Every number below was measured against the real server, not assumed.

---

Update **`Komora Draft Cart.dc.html`** in this project. The visual design is approved
and should survive — this is a correctness pass, not a redesign.

## Keep exactly as-is

The palette and both themes, Onest + IBM Plex Mono, the phone frame and status bar, the
`ТЕМА`/`СТАН` harness, the banner shapes, the row layout, the toast-with-«Повернути»,
the budget bar, the sticky summary, the `МОЛ`/`ХЛБ` tile placeholders, and the trust
footer. The `sc-if` / `sc-for` / `DCLogic` / `data-props` structure stays.

## 1. Komora now deletes things — the sheet's central promise is false

The confirm sheet says, in a green reassurance block:

> **Нічого не видалимо.** Ваші позиції в Сільпо залишаться на місці — ми лише додаємо.

That is no longer true. Komora removes products it previously added, so that «заміни
ковбаски на салямі» does not leave the sausage sitting next to its replacement.
Verified live 2026-08-18: the sausage left the real cart, the milk was untouched.

**Removal is the only irreversible thing this product does.** Adding to a cart is two
taps to undo in the Silpo app; a deletion is not. So:

- The green «нічого не видалимо» block must be **conditional** — shown only when the
  confirmation genuinely removes nothing.
- Add a **removal state** for the sheet that is the opposite of reassuring. It must
  name every product being taken out, and it should carry more visual weight than
  anything else on the sheet. This is the one place the design should *create*
  friction rather than dissolve it.
- Removals are never silent and never a surprise: they are named on the sheet before
  the tap that sends them.

Real copy from the shipped bot, for reference:

> **Приберемо з кошика:** Ковбаски Глобино Салямки з сиром Cheddar с/к.
> Це те, що Комора додала раніше. Решти у вашому кошику не торкаємось.

Note the wording shift — with a removal pending, the promise becomes «**решти** не
чіпаємо», not «не чіпаємо».

## 2. The sheet adds two numbers that cannot be added

The sheet currently renders:

```
Зараз у Сільпо        4 позиції · 312,40 ₴
Додаємо з Комори      10 позицій · 748,20 ₴
──────────────────────────────────────────
Разом у кошику Сільпо 14 позицій · 1 060,60 ₴
```

The total line is a confident number Komora cannot compute. The two operands are
different kinds of quantity:

- **Зараз у Сільпо** is Silpo's own `totalAfterDiscounts`. Measured 2026-08-18, it
  already has the account's −10% coupon applied to every line **not already on
  promotion**, plus a flat **9,00 ₴** that does not scale with basket size: 41,99 ₴ of
  milk read back as 50,99 ₴, and six lines summing to 522,56 ₴ read back as 531,56 ₴ —
  the same 9,00 both times.
- **Додаємо з Комори** is the plain sum of catalogue prices, because Komora
  deliberately never predicts a coupon. Silpo applies those itself.

Adding them overstates by the coupon and understates by the flat charge.

**Change:** drop the summed total line. Label each figure for what it is — «до сплати»
for Silpo's own number, «за цінами каталогу» for what is being added — and state that
the final sum is Silpo's to compute. Do **not** name the 9,00 ₴: a flat difference
measured twice is not the same as knowing what it is for.

The shipped bot now says:

> У вашому кошику Сільпо вже 1 позиція — до сплати 50,99 ₴, не чіпаємо.
> Додаємо 5 позицій — 533,97 ₴ за цінами каталогу.
> Це різні суми — остаточну рахує Сільпо вже в кошику.

## 3. The success screen assumes a checkout link that often does not exist

The success view offers **«Відкрити Сільпо»** as the primary action. Silpo issues
`checkoutWebLink` only for a cart it judges ready, and it does not always explain why
it withheld one. Measured 2026-08-18: a cart of **531,56 ₴** carried **no link at all**
and exactly one validation, at *info* level, about a payment type needing a 1000 ₴
minimum — nothing that counts as an error.

Add two success variants:

- **Link present** — as designed today.
- **No link, no reason** — no primary button to nowhere. Say where to finish instead:
  «Оформити замовлення — у застосунку або на сайті Сільпо.» Naming no cause is honest;
  leaving the user at a dead end is not.

Also correct the success subtitle. It says «купони застосуються на касі», but the −10%
is already applied in the cart, before checkout. Say that the cart price may differ
from the draft because Silpo applies its own discounts.

## 4. A checkout blocker state is missing entirely

Silpo returns error-level `calculation.validations[]` that stop checkout. These are
**codes**, translated for display. The three observed live:

- `order.cost.min` — order below the store's minimum
- `product.offer.stock.max` — a line exceeds remaining stock
- `timeslot.not_available` — the cart's delivery slot has expired

Add a panel — **«Сільпо не дасть оформити замовлення»** — listing the translated
reasons. It appears on the confirm sheet *and* on the success screen, because a cart
can be written successfully and still not be checkout-ready. It should read as
information the user must act on in the Silpo app, not as a Komora failure.

## 5. Weighted goods break the row and the stepper

Quantity is a **float**, not a count. A weighted product is priced per kilogram and
ordered in kilograms. Real example from 2026-08-18:

```
Ковбаса «Премія»® Салямі Золотиста    0,15 × 999,00 ₴/кг = 149,85 ₴
```

Two problems:

- The row prints `{{ r.unit }} · {{ r.priceFmt }}` — «900 мл · 42,90 ₴» — which for a
  weighted good reads as if 999,00 ₴ were the price of the piece. It needs an explicit
  per-unit form: **999,00 ₴/кг**.
- The `− 2 +` stepper assumes integers. Weighted goods move by a **step** (0,15 кг for
  this salami, 0,1 кг for parmesan, 0,25 кг for sliced bacon), and an unqualified
  request becomes exactly one step — not one kilogram. A user tapping `+` should get
  0,30, not 1.

Add a weighted row variant: quantity shown as «0,15 кг», price as «за кг», and a
stepper that moves by the product's step.

## 6. The savings model does not exist

`renderVals` maps coupons to products — `{id:'kav', kind:'pct', v:0.25}` → «Купон −25%
на вашу каву». Silpo publishes no such mapping. `get_my_coupons` and
`get_coupon_details` return a reward value plus prose and **no eligible-product list**,
so attributing a coupon to a line is invention.

What is real and machine-readable is **`oldPrice − price` per line**. Rework the
savings sheet around that:

- Per-line discounts, each naming the product and the amount actually saved — this is
  what the bot shows today: «Молоко пастеризоване «Яготинське» 2,6% — знижка 18,00 ₴».
- The user's coupons listed **separately, as information**, never attributed to a
  product and never summed into the total. A coupon's own description can be a
  fragment («на онлайн чек»), so it may be paired with its cap («Максимальна знижка
  100 грн»).

Keep the green treatment and the «остаточна може відрізнятись» caveat — both are right.

## 7. Two states the draft screen cannot currently show

- **«Не знайшлося»** — the model asked for something and nothing resolved. Distinct
  from «Чернетка порожня», which means the user removed everything. A draft can arrive
  with lines *and* a not-found warning for the ones that failed.
- **Unavailable, kept visible** — a product Silpo no longer offers stays on screen,
  struck through, **excluded from the total and never sent**, so the user can see what
  is missing. The current file expresses this only as "user reverted a substitution";
  it needs to exist on its own.

## 8. «⇄ Інший варіант» is a core interaction with no design

Every line offers the next candidate Silpo returns for the same query, cycling and
wrapping. In the bot it is a `⇄ N` keyboard; in the Mini App it belongs on the row.
This is a different verb from the existing «Повернути»/«Замінити» on a substitution —
that reverts a swap Komora made, this asks for a different product entirely.

## 9. Data model drift

- `reason_kind` is exactly `stated | habit | deal | meal | sub`. The file invents
  `warn` and `weak` and omits `stated` — which is **the only one that exists today**,
  since the habits engine is Plan 3. Every current line is `stated`, with a
  model-written Ukrainian sentence as its reason («Основний інгредієнт для тіста на
  піцу»). Keep the habit/deal styling for later, but make `stated` the default and the
  demo data reflect it.
- `qty` is a float.
- `optional` is real and drives budget trimming — keep exactly as designed.
- Dietary-restriction filtering was **removed** from the product. If any sibling screen
  implies Komora filters allergens, that must go: it would promise protection that does
  not exist.

## New `СТАН` presets to add to the harness

`removal` (a replacement that deletes a synced product) · `weighted` (salami by the
kilo) · `blocked` (checkout-blocking validations) · `nolink` (synced, no checkout link)
· `notfound` (some lines resolved, some did not).
