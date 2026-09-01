"""What a handler decides, before anyone decides how to say it.

Handlers used to return a `Reply` — Telegram HTML plus a Telegram callback keyboard —
which made the claim that they were "the seam the Mini App will use" untrue in the one
way that matters: a second surface needs the *cart*, not markup describing it.

So a handler returns one of these instead. They carry domain objects and plain prose,
no HTML and no callback strings. `bot/render.py: to_reply` turns one into a Telegram
message; a Mini App serialises the same object and draws its own.

Prose stays plain rather than becoming a code. A message like «Сільпо зараз не
відповідає» is as displayable in a web view as in a chat, and inventing an enum for
every sentence would buy nothing today. If the Mini App ever needs to style these
differently, that is the moment to give them kinds — not before.
"""

from dataclasses import dataclass

from komora.core.models import ResolvedCart, ResolvedLine, SyncReport
from komora.core.sync import SyncPreview


@dataclass(frozen=True)
class DraftReady:
    """A reviewed basket, waiting for the user to send it or change it."""

    title: str
    cart: ResolvedCart
    budget_cap: int | None = None
    basket_id: int | None = None
    """`None` when the draft was never persisted — nothing found, nothing to act on,
    so no keyboard. The cart is still worth showing: it carries the warnings that say
    why it is empty."""
    toast: str | None = None
    """Set after a swap, where the change is easy to miss in a re-rendered basket."""


@dataclass(frozen=True)
class PreviewReady:
    """The confirmation sheet: the live cart read back, before anything is written."""

    basket_id: int
    preview: SyncPreview


@dataclass(frozen=True)
class Synced:
    """What actually landed in the real Silpo cart."""

    basket_id: int
    report: SyncReport


@dataclass(frozen=True)
class Spoke:
    """Prose: an answer, a prompt, a refusal, a piece of state."""

    text: str
    needs_link: bool = False
    """Offer account linking. The only button this outcome can ask for, because it is
    the only one that is not about a basket."""
    toast: str | None = None


Outcome = DraftReady | PreviewReady | Synced | Spoke


@dataclass(frozen=True)
class AlternativesReady:
    """The products offered for one line, for a surface that can draw a list.

    **Deliberately not an `Outcome`.** An outcome is a turn in the conversation, and
    `render.to_reply` must be able to say every one of them in a chat. This is not a
    turn — it is a lookup a screen makes on its way to an edit, and the answer is a
    row of tappable products. A Telegram keyboard cannot draw that: the labels are
    full Ukrainian product names. So the chat keeps «⇄», which cycles one step at a
    time, and this stays the Mini App's. Putting it in the union would have forced a
    rendering into `to_reply` that nothing could ever reach.

    A refusal still comes back as a `Spoke`, so every gate answers the same way it
    does everywhere else.
    """

    basket_id: int
    position: int
    current: ResolvedLine
    options: list[ResolvedLine]
    """Ordered best-first, current product excluded. Empty means Silpo offered none —
    which the surface must show without leaving the draft."""
