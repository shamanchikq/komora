# Follow-up prompt for Claude Design — `Komora Draft Cart.dc.html`

Second pass, after reviewing the correctness pass. Everything from round one landed;
two things are better than specified (the confirm button naming the deletion, and the
weighted stepper matching the backend's 3-decimal rounding). Four gaps remain — one was
missing from my previous prompt, not from your work.

---

Four states are still missing. The visual design stays untouched again; these all reuse
patterns already in the file.

## 1. «⇄ Інший варіант» tapped, and there is nothing to offer

The ⇄ button currently renders only on rows where the demo data has candidates
(`r.hasAlts`). That reads honestly in a mockup, but the real app cannot know it in
advance: alternatives come from a **live search performed when the button is tapped**,
and the code gives up only after seeing the result —

```python
candidates = narrow(found, shelf)
if len(candidates) < 2:
    return None          # nothing else to offer
```

Pre-computing `hasAlts` for every row would cost one search per line at draft time, on
a screen that is meant to appear immediately. So the button is shown **optimistically,
on every row that is not unavailable**, and the empty result is a state.

Add it. The bot's copy today:

> **Інших варіантів для «Молоко Яготинське 2,6%» Сільпо не пропонує.**
> *(toast: «Інших варіантів нема»)*

The existing toast component is the right home — it already appears above the summary
bar and auto-dismisses. This one has no «Повернути» action; it is information, not an
undo. The row itself must not change: no flicker, no reorder, nothing struck.

Keep `hasAlts` for the demo harness if useful, but the button's *visibility* should no
longer depend on it — only `unavailable` suppresses it.

## 2. Degraded modes — three banners, not zero

Silpo can answer some questions and fail others. When that happens the cart is still
built and still worth sending; the user is told what could not be checked. The pipeline
emits exactly three of these, and every one already has Ukrainian copy in the shipped
bot:

| code | what failed | copy |
|---|---|---|
| `degraded:coupons` | `get_my_coupons` unreachable | Купони зараз недоступні — показано без них. |
| `degraded:replacements` | `get_replacements` unreachable | Сільпо не підказав заміни — деякі позиції могли бути кращими. |
| `degraded:verification` | the model could not re-check the picks | Не вдалося перевірити, чи товари відповідають запиту — перегляньте позиції уважніше. |

These are **not errors and not blockers.** The draft is complete and sendable. They
belong on the draft screen as a quiet, low-contrast banner — closer to the dashed
«мало даних» treatment than to the amber blocker panel, which is reserved for things
Silpo will refuse.

They can appear together, so the banner should take a list. And the third one changes
what the user should *do* — it is the only case where Komora is saying "we could not
confirm these are right, look properly", which is worth a slightly firmer weight than
the other two.

## 3. The draft screen has no timeslot warning

`timeslot.not_available` is handled as a **sheet blocker**, which is right. But the same
condition also surfaces one step earlier, as a warning on the **draft** — Silpo is asked
about the slot while the cart is being built, so the user can learn about it before
tapping send rather than after:

> Час доставки у вашому кошику Сільпо вже недоступний. Кошик зберемо, але оберіть новий
> час у застосунку Сільпо перед оформленням.

Note the shape: the cart is still built, still shown, still sendable. This is a warning
on the draft, not a refusal — the refusal comes later, from Silpo, and that is already
designed. Use the same quiet banner as the degraded modes.

## 4. One line of copy overstates

The removal panel says:

> Повернути видалене можна **тільки вручну** в Сільпо.

Not quite true — the user can also just ask Komora to add it back in chat. The instinct
is right (a deletion should feel weighty and the panel should not offer a soft undo),
but the sentence claims a constraint that does not exist. Something closer to: Komora
will not undo this for you, and the product will not come back on its own.

---

## Not changing

The ⇄ gating question aside, everything from the correctness pass stands. In
particular, keep: the inverted removal panel and its position as the heaviest element,
the two labelled figures with no sum, the link/no-link success split, the amber blocker
panel, the weighted row and step-based stepper, savings built on `oldPrice − price`
with coupons listed separately, and `merge()` refusing to inherit an old price.
