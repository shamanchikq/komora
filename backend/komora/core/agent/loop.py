"""The agent loop: the model decides, the pipeline acts.

Four safeguards, each earned rather than speculative:

* **Write tools are unreachable.** Not merely absent from the declarations — a call to
  one raises, so a model that hallucinates a tool name cannot mutate a cart.
* **Repeated identical calls stop the loop.** A model that never sees tool output will
  re-issue the same call forever; burning every step on it just delays the failure.
* **A malformed basket is retried with the validation error.** Observed live:
  gemma4:12b returned `description: null` on every line under tool load. The retry
  hands the model the actual error rather than discarding the turn.
* **A mistyped read call goes back to the model too.** Models guess parameter names —
  every name assumed from a tool name in this project turned out wrong. A `TypeError`
  from an unknown kwarg used to escape every catch layer and leave the user in
  silence.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import ValidationError

from komora.core.agent.prompts import SYSTEM_PROMPT
from komora.core.agent.tools import (
    CONTEXT_TOOLS,
    INJECTED_PARAMS,
    MAX_LISTED_PRODUCTS,
    PROPOSE_BASKET,
    READ_TOOLS,
)
from komora.core.llm.protocol import LLMClient, Message, ToolCall, ToolDecl
from komora.core.mcp.protocol import SilpoClient
from komora.core.models import DraftBasket, SearchContext

MAX_STEPS = 8
MAX_BASKET_RETRIES = 2

log = logging.getLogger(__name__)

_FALLBACK = "Не вдалося це опрацювати. Спробуйте сформулювати інакше."
_TOO_LONG = "Це зайняло надто багато кроків. Спробуйте, будь ласка, конкретніше."


class ForbiddenToolCall(Exception):
    """The model asked for a tool it must never reach — a write, or an invention."""


@dataclass(frozen=True)
class AgentOutcome:
    basket: DraftBasket | None = None
    reply: str | None = None


async def run_agent(
    *,
    llm: LLMClient,
    mcp: SilpoClient,
    context: SearchContext,
    history: Sequence[Message],
    user_message: str,
    tools: Sequence[ToolDecl],
    max_steps: int = MAX_STEPS,
) -> AgentOutcome:
    """Run one user turn to a basket or a reply.

    `tools` comes from `build_tool_decls(await mcp.list_tools())`. The caller builds it
    once per process rather than per turn: the declarations are static, and an
    identical prefix is what lets implicit context caching hit.
    """
    messages: list[Message] = [*history, Message("user", user_message)]

    last_call: tuple[str, str] | None = None
    basket_failures = 0

    for _ in range(max_steps):
        response = await llm.complete(system=SYSTEM_PROMPT, messages=messages, tools=tools)

        if not response.tool_calls:
            return AgentOutcome(reply=response.text or _FALLBACK)

        call = response.tool_calls[0]

        if call.name == PROPOSE_BASKET:
            try:
                basket = DraftBasket.model_validate({**call.args, "intent": "stated"})
                basket = basket.model_copy(update={"intent": intent_of(basket)})
            except ValidationError as exc:
                basket_failures += 1
                if basket_failures > MAX_BASKET_RETRIES:
                    return AgentOutcome(reply=_FALLBACK)
                messages.append(_assistant_turn(call))
                messages.append(_tool_result(call, _explain(exc)))
                continue
            return AgentOutcome(basket=basket)

        if call.name not in READ_TOOLS:
            raise ForbiddenToolCall(
                f"{call.name} is not a read tool; cart changes go through the pipeline"
            )

        fingerprint = (call.name, json.dumps(call.args, sort_keys=True, ensure_ascii=False))
        if fingerprint == last_call:
            return AgentOutcome(reply=response.text or _TOO_LONG)
        last_call = fingerprint

        try:
            result = await _dispatch(mcp, call, context)
        except TypeError as exc:
            # A guessed parameter name raises before Silpo is ever reached. The error
            # goes back the way a failed basket validation does; an unchanged repeat
            # stops at the identical-call guard above, so this stays bounded.
            log.warning("%s rejected its arguments: %s", call.name, exc)
            messages.append(_assistant_turn(call))
            messages.append(_tool_result(call, f"ПОМИЛКА в аргументах {call.name}: {exc}."))
            continue
        messages.append(_assistant_turn(call))
        messages.append(_tool_result(call, result))

    return AgentOutcome(reply=_TOO_LONG)


def intent_of(basket: DraftBasket) -> str:
    """What kind of basket the model proposed, from the fields that survived validation.

    One `propose_basket` serves every intent (Plan 4 D5/D7): a menu makes it a meal
    plan, a headcount makes it an event, and neither makes it the stated basket Plan 1
    shipped. The intent is stored with the draft and shown nowhere; it exists so a
    later reader can tell the three apart.

    Read off the validated basket, not the raw arguments: a headcount of 0 or 5000 is
    dropped by `DraftBasket` as no event at all, and the raw `guests` still called the
    basket one.
    """
    if basket.guests:
        return "event"
    if basket.menu:
        return "mealplan"
    return "stated"


def _assistant_turn(call: ToolCall) -> Message:
    return Message("assistant", tool_calls=(call,))


def _tool_result(call: ToolCall, payload: str) -> Message:
    return Message("tool", payload, tool_name=call.name, tool_call_id=call.id)


def _explain(error: ValidationError) -> str:
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()[:5]
    )
    return f"ПОМИЛКА у propose_basket: {problems}. Виправ і виклич ще раз."


async def _dispatch(mcp: SilpoClient, call: ToolCall, context: SearchContext) -> str:
    """Invoke a read tool, filling in the context parameters the model never sees.

    The injected names are stripped from the model's own arguments first: they are
    hidden from the schema, but a model that emits one anyway would otherwise pass it
    twice and raise a TypeError instead of answering.
    """
    method = getattr(mcp, READ_TOOLS[call.name])
    args = {k: v for k, v in call.args.items() if k not in INJECTED_PARAMS and k != "context"}

    if call.name == "silpo_find_products_batch":
        queries = args.pop("products", None) or args.pop("queries", None) or []
        result = await method(queries, context)
    elif call.name in ("silpo_get_product_details", "silpo_get_similar_products"):
        slug = str(args.pop("slug", ""))
        result = await method(slug, context, **args) if args else await method(slug, context)
    elif call.name in CONTEXT_TOOLS:
        result = await method(context, **args)
    else:
        result = await method(**args)

    return fit_for_model(result)


MAX_TOOL_RESULT = 8000
"""Characters of one tool result the model is shown."""

UNREAD_FIELDS: Final = frozenset({"image", "companyId", "branchId"})
"""Product fields no tool the model holds can use: a picture URL, and the two ids only
a cart write needs. A third of every product's characters — ~200 of ~540."""


def clip_products(result: Any, limit: int = MAX_LISTED_PRODUCTS) -> Any:
    """Shorten every product list in a result to `limit`, and say how many went.

    Both shapes: a browse's top-level `products`, and a search's `queries[].products`
    — the search is the one the model reaches for, thirty products per term by
    default, and it was never clipped at all.
    """
    if not isinstance(result, dict):
        return result
    out = dict(result)
    products = result.get("products")
    if isinstance(products, list) and len(products) > limit:
        out["products"] = products[:limit]
        out["omitted"] = len(products) - limit
    groups = result.get("queries")
    if isinstance(groups, list):
        out["queries"] = [clip_products(group, limit) for group in groups]
    return out


def _slim(result: Any) -> Any:
    if isinstance(result, list):
        return [_slim(item) for item in result]
    if not isinstance(result, dict):
        return result
    return {k: _slim(v) for k, v in result.items() if k not in UNREAD_FIELDS}


def _longest_list(result: Any) -> int:
    if not isinstance(result, dict):
        return 0
    lengths = [len(result["products"])] if isinstance(result.get("products"), list) else [0]
    for group in result.get("queries") or []:
        lengths.append(_longest_list(group))
    return max(lengths)


def fit_for_model(result: Any, budget: int = MAX_TOOL_RESULT) -> str:
    """A tool result as JSON the model can read to the end.

    The first clip counted products and then cut the JSON at 8 000 characters anyway:
    twenty products of a browse are ~11 000, so the cut still fell mid-object, and a
    search — thirty products per term — was cut after a dozen. Now the unreadable
    fields go first (`UNREAD_FIELDS`), then products come off the end of every list
    until the whole payload fits, each list saying how many it lost. Only a result
    with no product list to shorten can still be cut, as the last resort it was.
    """
    slim = _slim(result)

    def dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    text = dump(clip_products(slim))
    if len(text) <= budget:
        return text
    low, high = 0, min(_longest_list(slim), MAX_LISTED_PRODUCTS)
    best: str | None = None
    while low <= high:
        keep = (low + high) // 2
        trial = dump(clip_products(slim, keep))
        if len(trial) <= budget:
            best, low = trial, keep + 1
        else:
            high = keep - 1
    return best if best is not None else text[:budget]
