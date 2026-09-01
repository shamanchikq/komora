"""The Mini App API: the same Outcomes the bot renders, over HTTP instead of markup.

Task 0 made handlers return domain objects precisely so this file could exist: each
route calls the plain handler functions and serialises the result. No rendering rules
are re-implemented here — the frontend draws from the domain objects directly.

**What does not carry over from the bot, rebuilt here:** ownership and confirmation.
Both live in the handlers themselves (`_own_draft` gates every basket action), so an
HTTP caller inherits them rather than bypassing them. What this layer adds is identity:
every route requires `Authorization: tma <initData>`, verified against the bot token —
an unauthenticated endpoint would let anyone act on anyone's cart.
"""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from komora.bot.handlers import (
    Services,
    on_cancel,
    on_choose_alternative,
    on_list_alternatives,
    on_open_active,
    on_open_basket,
    on_preview,
    on_push,
    on_remove_line,
    on_set_qty,
    on_swap,
    on_text,
    on_trim_optional,
)
from komora.bot.outcomes import (
    AlternativesReady,
    DraftReady,
    Outcome,
    PreviewReady,
    Spoke,
    Synced,
)
from komora.core.initdata import InitDataRejected, verify_init_data


def serialise(outcome: Outcome | AlternativesReady) -> dict[str, Any]:
    """One JSON shape per outcome kind; `kind` is how the frontend tells them apart.

    `AlternativesReady` rides along without being an `Outcome`: it is a lookup this
    surface makes, not a turn in the conversation, and `render.to_reply` has no way to
    draw a row of tappable products in a chat. See `bot/outcomes.py`.
    """
    match outcome:
        case AlternativesReady():
            return {
                "kind": "alternatives",
                "basket_id": outcome.basket_id,
                "position": outcome.position,
                "current": outcome.current.model_dump(mode="json"),
                "options": [option.model_dump(mode="json") for option in outcome.options],
            }
        case DraftReady():
            return {
                "kind": "draft",
                "basket_id": outcome.basket_id,
                "title": outcome.title,
                "budget_cap": outcome.budget_cap,
                "cart": outcome.cart.model_dump(mode="json"),
                "toast": outcome.toast,
            }
        case PreviewReady():
            return {
                "kind": "preview",
                "basket_id": outcome.basket_id,
                "preview": outcome.preview.model_dump(mode="json"),
            }
        case Synced():
            return {
                "kind": "synced",
                "basket_id": outcome.basket_id,
                "report": outcome.report.model_dump(mode="json"),
            }
        case Spoke():
            return {
                "kind": "spoke",
                "text": outcome.text,
                "needs_link": outcome.needs_link,
                "toast": outcome.toast,
            }
    raise TypeError(f"unhandled outcome {type(outcome).__name__}")


def _authenticator(bot_token: str) -> Callable[[str | None], int]:
    def telegram_user(
        authorization: Annotated[str | None, Header()] = None,
    ) -> int:
        """`Authorization: tma <initData>` — the scheme Telegram itself documents.

        initData is frozen at launch, so freshness is judged against `auth_date`
        inside `verify_init_data`; a replayed payload within the window is the
        accepted exposure of this scheme.
        """
        payload = authorization.removeprefix("tma ") if authorization else ""
        try:
            return verify_init_data(payload.strip(), bot_token)
        except InitDataRejected as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    return telegram_user


class DraftIn(BaseModel):
    text: str


class SwapIn(BaseModel):
    position: int


class ChooseIn(BaseModel):
    product_id: str
    """Checked against a freshly built candidate list, never trusted — see
    `handlers.on_choose_alternative`."""


class QtyIn(BaseModel):
    qty: float
    """Refused in the handler rather than here: rejecting `NaN` at this layer answers
    with a 422 whose body quotes the offending value, and that body is itself not
    JSON — one 500 traded for another. `on_set_qty` says it in Ukrainian instead."""


def minapp_router(services: Services, bot_token: str) -> APIRouter:
    router = APIRouter(prefix="/api")
    User = Annotated[int, Depends(_authenticator(bot_token))]

    @router.post("/draft")
    async def draft(body: DraftIn, user_id: User) -> dict[str, Any]:
        """A free-text turn — the same conversation the bot has."""
        return serialise(await on_text(services, user_id, body.text))

    @router.get("/baskets/active")
    async def active(user_id: User) -> dict[str, Any]:
        """The draft this user has open, if any — what a payload-less launch asks for.

        Declared **before** `/baskets/{basket_id}`: Starlette matches in order, and
        "active" would otherwise be tried as an int and 422 before reaching here.

        No id crosses the wire, so there is nothing to own — the draft is looked up by
        the sender. A user with none gets a `spoke`, which the app reads as "open on
        compose" rather than as a destination.
        """
        return serialise(await on_open_active(services, user_id))

    @router.get("/baskets/{basket_id}")
    async def open_basket(basket_id: int, user_id: User) -> dict[str, Any]:
        """Where a deep link lands: show a basket that already exists.

        The only GET here, because it is the only route that does not act. The id
        comes out of the launch payload, which is signed but chosen by whoever opened
        the link — so it goes through the same ownership gate as everything else.
        """
        return serialise(await on_open_basket(services, user_id, basket_id))

    @router.post("/baskets/{basket_id}/preview")
    async def preview(basket_id: int, user_id: User) -> dict[str, Any]:
        """Read the live Silpo cart back into a confirmation sheet. First tap."""
        return serialise(await on_preview(services, user_id, basket_id))

    @router.post("/baskets/{basket_id}/push")
    async def push(basket_id: int, user_id: User) -> dict[str, Any]:
        """Write the draft into the real Silpo cart. Second tap — never implied."""
        return serialise(await on_push(services, user_id, basket_id))

    @router.post("/baskets/{basket_id}/swap")
    async def swap(basket_id: int, body: SwapIn, user_id: User) -> dict[str, Any]:
        return serialise(await on_swap(services, user_id, basket_id, body.position))

    @router.post("/baskets/{basket_id}/lines/{position}/qty")
    async def set_qty(basket_id: int, position: int, body: QtyIn, user_id: User) -> dict[str, Any]:
        """A stepper tap — persisted against the draft, clamped server-side."""
        return serialise(await on_set_qty(services, user_id, basket_id, position, body.qty))

    @router.post("/baskets/{basket_id}/lines/{position}/remove")
    async def remove_line(basket_id: int, position: int, user_id: User) -> dict[str, Any]:
        """✕ on a row. Edits the draft only; Silpo is still behind the two taps."""
        return serialise(await on_remove_line(services, user_id, basket_id, position))

    @router.get("/baskets/{basket_id}/lines/{position}/alternatives")
    async def alternatives(basket_id: int, position: int, user_id: User) -> dict[str, Any]:
        """What else Silpo has for this line. Reads only — nothing is chosen here."""
        return serialise(await on_list_alternatives(services, user_id, basket_id, position))

    @router.post("/baskets/{basket_id}/lines/{position}/choose")
    async def choose(
        basket_id: int, position: int, body: ChooseIn, user_id: User
    ) -> dict[str, Any]:
        """Put one of those products on the line. The id is re-checked, not trusted."""
        return serialise(
            await on_choose_alternative(services, user_id, basket_id, position, body.product_id)
        )

    @router.post("/baskets/{basket_id}/trim")
    async def trim(basket_id: int, user_id: User) -> dict[str, Any]:
        """Drop every optional line still sendable."""
        return serialise(await on_trim_optional(services, user_id, basket_id))

    @router.post("/baskets/{basket_id}/cancel")
    async def cancel(basket_id: int, user_id: User) -> dict[str, Any]:
        return serialise(await on_cancel(services, user_id, basket_id))

    return router
