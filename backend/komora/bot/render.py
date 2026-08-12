"""Ukrainian text for everything the bot says about a cart.

Pure functions over domain objects — no aiogram, no I/O — so the wording is testable
and the Mini App can reuse the same rules later.

Two things are load-bearing rather than cosmetic:

* **Every line shows its reason.** Nothing appears in a cart without a visible one.
* **Warnings are rendered, never dropped.** The passes emit codes (`not_found:…`,
  `degraded:coupons`) precisely so nothing is lost between the pipeline and the user;
  an unrecognised code is still shown verbatim rather than hidden.

Output is Telegram HTML, so every value that came from Silpo or the model goes through
`esc()` — product names contain `&` and «» routinely.
"""

from decimal import Decimal
from html import escape

from komora.core.models import ResolvedCart, ResolvedLine, SyncReport
from komora.core.money import CURRENCY, uah
from komora.core.passes.budget import optional_lines_total
from komora.core.sync import SyncPreview

money = uah
"""Re-exported: the same rule the savings notes are written with."""


def esc(value: object) -> str:
    return escape(str(value), quote=False)


def pl(n: float, one: str, few: str, many: str) -> str:
    """Ukrainian plural: 1 позиція, 2 позиції, 5 позицій, 11 позицій, 21 позиція."""
    count = abs(int(n))
    if count % 100 in range(11, 15):
        return many
    last = count % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


def items(n: int) -> str:
    return f"{n} {pl(n, 'позиція', 'позиції', 'позицій')}"


def quantity(qty: float) -> str:
    """Whole numbers stay whole; weights keep their decimals (0,5 кг, not 0.500)."""
    text = f"{qty:.3f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


def line_text(index: int, line: ResolvedLine) -> str:
    name = esc(line.name)
    unit = f" · {esc(line.unit)}" if line.unit else ""
    parts = [f"{index}. <b>{name}</b>{unit}"]

    if line.unavailable:
        parts.append("   ✕ немає в наявності — не враховано в сумі")
    else:
        parts.append(
            f"   {quantity(line.qty)} × {money(line.unit_price)} = <b>{money(line.line_total)}</b>"
        )
        if line.old_price and line.old_price > line.unit_price:
            parts.append(f"   ↓ було {money(line.old_price)}")

    if line.substituted_from:
        parts.append(f"   ⇄ заміна (було: {esc(line.substituted_from)})")
    if line.optional:
        parts.append("   ○ необовʼязково")
    parts.append(f"   — {esc(line.reason_text)}")
    return "\n".join(parts)


def warning_text(code: str) -> str:
    """Translate a pass warning into something a person can act on.

    An unknown code falls through verbatim: a warning that is not understood still has
    to be seen.
    """
    kind, _, rest = code.partition(":")
    if kind == "not_found":
        return f"Не знайшлося: «{esc(rest)}»"
    if kind == "excluded":
        item, _, restriction = rest.rpartition(":")
        return f"Прибрано через ваші обмеження: «{esc(item)}» ({esc(restriction)})"
    if kind == "over_budget":
        return f"Понад тижневий бюджет на {money(Decimal(rest))}"
    if kind == "timeslot":
        return (
            "Час доставки у вашому кошику Сільпо вже недоступний. Кошик зберемо, але "
            "оберіть новий час у застосунку Сільпо перед оформленням."
        )
    if kind == "degraded":
        return {
            "coupons": "Купони зараз недоступні — показано без них.",
            "replacements": "Сільпо не підказав заміни — деякі позиції могли бути кращими.",
            "restrictions": "Не вдалося перевірити ваші харчові обмеження.",
        }.get(rest, f"Часткові дані: {esc(rest)}")
    return esc(code)


_VALIDATIONS: dict[str, str] = {
    "timeslot.not_available": (
        "Час доставки у вашому кошику Сільпо вже недоступний — оберіть новий "
        "у застосунку Сільпо, інакше замовлення не оформиться."
    ),
    "product.offer.stock.max": (
        "У кошику Сільпо є позиція, якої лишилося менше, ніж замовлено — "
        "зменште кількість у застосунку."
    ),
    "order.cost.min": (
        "Сума замовлення менша за мінімальну для цього магазину — "
        "додайте ще щось, інакше Сільпо не прийме замовлення."
    ),
}
"""`calculation.validations[].message` is a **code**, not prose.

**Only codes actually observed live belong here.** An earlier version also listed
`order.min_sum` and `product.offer.not_available`, which were invented from the shape
of the real ones — and the code Silpo actually sends for a too-small order turned out
to be `order.cost.min`, found in the first Telegram run. A guessed key never fires; it
just makes the table look more complete than it is.

Observed so far: `timeslot.not_available`, `product.offer.stock.max`, `order.cost.min`,
and `promotion.available` at info level.
"""


def validation_text(code: str) -> str:
    """An untranslated code is still shown — a checkout blocker must never be hidden
    just because nobody has written the Ukrainian for it yet."""
    known = _VALIDATIONS.get(code.strip())
    return known if known else f"Сільпо повідомляє про перешкоду: {esc(code)}"


SWAP_HINT = "⇄ 1…N — показати інший варіант для цієї позиції"


def render_cart(
    cart: ResolvedCart,
    title: str,
    *,
    budget_cap: int | None = None,
    swappable: bool = False,
) -> str:
    """The draft the user reviews before anything reaches Silpo.

    `swappable` adds the one line that explains the «⇄ N» keyboard. A row of arrows
    with no caption reads as decoration, and a control nobody understands is a control
    nobody uses.
    """
    removals = (
        ["", f"<b>Приберемо з кошика Сільпо:</b> {', '.join(esc(r.name) for r in cart.removals)}"]
        if cart.removals
        else []
    )

    if not cart.lines:
        # «прибери ковбаски» is a whole basket: nothing to add, something to take out.
        # Only a basket with neither has failed.
        body = removals or ["", "Нічого не вдалося підібрати."]
        return "\n".join(
            [f"<b>{esc(title)}</b>", *body, *(warning_text(w) for w in cart.warnings)]
        ).strip()

    blocks = [f"<b>{esc(title)}</b>", ""]
    blocks += [line_text(i, line) for i, line in enumerate(cart.lines, start=1)]
    blocks += removals

    counted = [ln for ln in cart.lines if not ln.unavailable]
    blocks += ["", f"<b>Разом: {money(cart.total)}</b> · {items(len(counted))}"]

    if cart.estimated_savings > 0:
        blocks.append(f"Заощаджено ≈ {money(cart.estimated_savings)}")
    if budget_cap is not None:
        left = Decimal(budget_cap) - cart.total
        blocks.append(
            f"Бюджет {budget_cap} {CURRENCY} — лишається {money(left)}"
            if left >= 0
            else f"Бюджет {budget_cap} {CURRENCY} — перевищено на {money(-left)}"
        )
        trimmable = optional_lines_total(cart)
        if left < 0 and trimmable > 0:
            blocks.append(
                f"Необовʼязкові позиції — це {money(trimmable)}. "
                "Це ваш вибір: надіслати можна попри це."
            )

    if swappable and len(cart.lines) > 1:
        blocks.append(SWAP_HINT)
    if cart.warnings:
        blocks += ["", *(warning_text(w) for w in cart.warnings)]
    notes = [*cart.savings_notes, *cart.coupon_notes]
    if notes:
        blocks += ["", "<b>Знижки та купони</b>", *(f"• {esc(n)}" for n in notes[:8])]

    return "\n".join(blocks)


def render_sync_preview(preview: SyncPreview) -> str:
    """The confirmation sheet. Its whole job is to promise nothing gets removed — and
    to be exact about the one case where a quantity is replaced rather than added."""
    blocks = ["<b>Надіслати в Сільпо?</b>", ""]

    if preview.existing_count:
        # «не чіпаємо» is a promise, so it is only made when it is true — with a
        # removal pending, the exception is named a few lines down instead.
        rest = "решти не чіпаємо" if preview.removing else "не чіпаємо"
        blocks.append(
            f"У вашому кошику Сільпо вже {items(preview.existing_count)} "
            f"на {money(preview.existing_total)} — {rest}."
        )
    else:
        blocks.append("Ваш кошик Сільпо зараз порожній.")

    if preview.adding_count:
        blocks.append(f"Додаємо {items(preview.adding_count)} на {money(preview.adding_total)}.")

    if preview.removing:
        names = ", ".join(esc(n) for n in preview.removing)
        blocks += [
            "",
            f"<b>Приберемо з кошика:</b> {names}.",
            "Це те, що Комора додала раніше. Решти у вашому кошику не торкаємось.",
        ]

    if preview.overlapping:
        names = ", ".join(esc(n) for n in preview.overlapping)
        blocks += [
            "",
            f"Уже в кошику: {names}. Кількість буде <b>замінено</b>, а не додано.",
        ]

    if preview.now_unavailable:
        names = ", ".join(esc(n) for n in preview.now_unavailable)
        blocks += ["", f"Уже немає в наявності: {names}. Сільпо може їх не додати."]

    if preview.drift is not None:
        confirmed, current = preview.drift
        blocks += [
            "",
            f"Ціни змінилися: було {money(confirmed)}, зараз {money(current)}.",
        ]

    if preview.blocking_validations:
        blocks += ["", "<b>Сільпо не дасть оформити замовлення:</b>"]
        blocks += [f"• {validation_text(v)}" for v in dict.fromkeys(preview.blocking_validations)]

    return "\n".join(blocks)


def render_sync_report(report: SyncReport) -> str:
    """What actually landed. A partial sync is never dressed up as a success."""
    removed_line = (
        f"Прибрано: {', '.join(esc(n) for n in report.removed)}." if report.removed else ""
    )

    if report.ok and report.added:
        blocks = [f"<b>Готово.</b> Додано {items(len(report.added))} у кошик Сільпо."]
        if removed_line:
            blocks.append(removed_line)
    elif report.ok and report.removed:
        blocks = [f"<b>Готово.</b> {removed_line}"]
    elif report.ok:
        blocks = ["Нічого додавати — кошик порожній."]
    else:
        blocks = ["<b>Вийшло не все.</b>"]
        if report.added:
            blocks.append(f"Додано: {', '.join(esc(n) for n in report.added)}")
        if removed_line:
            blocks.append(removed_line)
        if report.failed:
            blocks.append("Не вдалося додати:")
            blocks += [f"• {esc(name)} — {esc(error)}" for name, error in report.failed]
        if report.remove_failed:
            blocks.append("Не вдалося прибрати — ці позиції лишилися в кошику:")
            blocks += [f"• {esc(name)} — {esc(error)}" for name, error in report.remove_failed]
        blocks.append("")
        blocks.append("Можна спробувати ще раз — повторне надсилання нічого не подвоїть.")

    if report.checkout_mobile_link:
        # Telegram only accepts http(s) in inline buttons, so a `silpo://` deep link
        # can only be text. Shown anyway: Silpo's own guidance is to offer both.
        blocks += ["", f"У застосунку Сільпо: {esc(report.checkout_mobile_link)}"]

    if report.blocking_validations and not report.checkout_web_link:
        # Live: a cart holding a line that exceeds stock gets no checkout link at all.
        # "Готово" with no link and no reason leaves the user with nowhere to go.
        blocks += ["", "<b>Оформити поки не вийде:</b>"]
        blocks += [f"• {validation_text(v)}" for v in dict.fromkeys(report.blocking_validations)]

    return "\n".join(blocks)
