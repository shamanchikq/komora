"""What the model is told it did last turn.

The conversation history used to record a draft as its title alone — «[чернетка]
Інгредієнти для піци пепероні». Asked next turn to «заміни ковбаски на ковбасу
салямі», the model had no idea what was in the basket it was being asked to edit, so
it rebuilt one from the intent and quietly changed lines nobody mentioned: a tomato
sauce became tinned tomatoes on a turn that was only about sausage.

So a draft is recorded as what it actually contains, and a sync as what actually
landed in the real Silpo cart. Both are cheap — they are text this process already
has, not another model request — and together they are what makes an edit possible:
the model can only name a product for removal if it can see the product.

Written for a reader that is a model, not a person. `bot/render.py` owns everything
the user sees.
"""

from komora.core.models import ResolvedCart, SyncReport

DRAFT_TAG = "[чернетка]"
SYNCED_TAG = "[надіслано в кошик Сільпо]"
"""The prompt names this tag: seeing it is how the model knows the products are
already in the real cart and that an edit needs `removals` rather than new lines."""

MAX_ITEMS = 15
"""Twenty of these share the context window. A basket longer than this is already
past what a person reviews in a chat message."""

MAX_NAME = 80


def _clip(text: str, limit: int = MAX_NAME) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _listing(names: list[str]) -> str:
    shown = "; ".join(_clip(name) for name in names[:MAX_ITEMS])
    rest = len(names) - MAX_ITEMS
    return f"{shown} (+ще {rest})" if rest > 0 else shown


def draft_recap(title: str, cart: ResolvedCart) -> str:
    """The draft as the model needs to see it: the query it wrote, and what that found.

    Both halves matter. The description is what it must reuse to keep an unchanged line
    unchanged; the product name is what the user is looking at on screen and will refer
    to when asking for something else.
    """
    lines = [f"{DRAFT_TAG} {_clip(title, 120)}"]
    for index, line in enumerate(cart.lines[:MAX_ITEMS], start=1):
        state = " — немає в наявності" if line.unavailable else ""
        quantity = f"{line.qty:g}"
        lines.append(
            f"{index}. {_clip(line.description, 60)} → {_clip(line.name)} × {quantity}{state}"
        )
    if len(cart.lines) > MAX_ITEMS:
        lines.append(f"…ще {len(cart.lines) - MAX_ITEMS}")
    return "\n".join(lines)


def sync_recap(report: SyncReport) -> str:
    """What is in the user's real Silpo cart now, and what left it.

    A partial sync is recorded as a partial sync. The model is the thing that will be
    asked to edit this cart next turn, and telling it a line landed when it did not
    would have it propose a removal for something that was never there.
    """
    parts: list[str] = []
    if report.added:
        parts.append(f"{SYNCED_TAG} {_listing(report.added)}")
    if report.removed:
        parts.append(f"[прибрано з кошика Сільпо] {_listing(report.removed)}")
    if report.failed:
        parts.append(f"[не додалося] {_listing([name for name, _ in report.failed])}")
    if report.remove_failed:
        parts.append(f"[не прибралося] {_listing([name for name, _ in report.remove_failed])}")
    return "\n".join(parts) or "[у кошику Сільпо нічого не змінилося]"
