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

from dataclasses import dataclass, field
from datetime import date, datetime

from komora.core.deals.models import Snapshot
from komora.core.deals.scan import BranchDeal
from komora.core.habits.engine import Habit
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


@dataclass(frozen=True)
class HabitsReady:
    """«Your usual»: what the engine tracks for this user, sentences included.

    The cadence sentence is built by the engine and carried here as text, so no
    surface restates a rule — the defect class every frontend review has found.
    Muted habits are included with their flag: a list that hid them would have no
    way to offer «стежити знову».
    """

    habits: list[Habit]
    today: date
    fresh_at: datetime | None = None
    """When history was last read successfully; `None` if never."""
    toast: str | None = None
    """Set after a mute or an unmute, so the list stays on screen and the change is
    still announced — a sentence replacing the screen is the lesson learned thrice."""


@dataclass(frozen=True)
class NudgeReady:
    """The one proactive message: due habits, offered as a draft. Never a cart write."""

    habits: list[Habit]
    today: date


@dataclass(frozen=True)
class Ask:
    """A yes-or-no before something that cannot be undone. `yes` is the action's
    callback payload; the surface draws the two buttons."""

    text: str
    yes: str
    yes_label: str
    no: str
    """The refusal's callback payload. Its own action, never a shared «dismiss»: that
    one answers a nudge («Нагадаю, коли знову буде пора»), and cancelling a wipe with
    it told the user they would be reminded about something."""
    no_label: str = "Скасувати"


@dataclass(frozen=True)
class TrackedDeal:
    """One of the household's own products, on promotion right now (Plan 4 J6)."""

    habit: Habit
    snapshot: Snapshot
    below_usual: int | None = None
    """Whole percent under the «звичайна ціна» — only once enough shelf snapshots
    exist (`deals.scan.USUAL_MIN_SNAPSHOTS`); `None` says nothing about it."""


@dataclass(frozen=True)
class DealReady:
    """The one proactive deal message: a tracked product is cheaper than its own
    old price. Exact, from `oldPrice − price`; never a coupon. Offers a draft."""

    deals: list[TrackedDeal]
    today: date


@dataclass(frozen=True)
class DealsReady:
    """«/deals» and the «Акції» screen: your products, this branch, coupons and promos.

    Every list is computed by Python from reads — no model request — and the branch
    list is ranked by discount here, never by Silpo's list order. `coupons` and
    `promos` are prose about the account, not about any product.
    """

    mine: list[TrackedDeal]
    branch: list[BranchDeal]
    coupons: list[str] = field(default_factory=list)
    promos: list[str] = field(default_factory=list)
    scanned_at: datetime | None = None
    """When the tracked products were last re-priced; `None` when never."""
    warnings: list[str] = field(default_factory=list)
    """`degraded:branch`, `degraded:coupons`, `degraded:promos` — a part Silpo did not
    answer, shown as such rather than as an empty list that looks like «no deals»."""
    toast: str | None = None


Outcome = (
    DraftReady
    | PreviewReady
    | Synced
    | Spoke
    | HabitsReady
    | NudgeReady
    | Ask
    | DealReady
    | DealsReady
)


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
