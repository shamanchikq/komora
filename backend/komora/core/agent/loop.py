"""The agent loop: the model decides, the pipeline acts.

Three safeguards, each earned rather than speculative:

* **Write tools are unreachable.** Not merely absent from the declarations — a call to
  one raises, so a model that hallucinates a tool name cannot mutate a cart.
* **Repeated identical calls stop the loop.** A model that never sees tool output will
  re-issue the same call forever; burning every step on it just delays the failure.
* **A malformed basket is retried with the validation error.** Observed live:
  gemma4:12b returned `description: null` on every line under tool load. The retry
  hands the model the actual error rather than discarding the turn.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from komora.core.agent.prompts import SYSTEM_PROMPT
from komora.core.agent.tools import (
    CONTEXT_TOOLS,
    INJECTED_PARAMS,
    PROPOSE_BASKET,
    READ_TOOLS,
)
from komora.core.llm.protocol import LLMClient, Message, ToolCall, ToolDecl
from komora.core.mcp.protocol import SilpoClient
from komora.core.models import DraftBasket, SearchContext

MAX_STEPS = 8
MAX_BASKET_RETRIES = 2

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

        result = await _dispatch(mcp, call, context)
        messages.append(_assistant_turn(call))
        messages.append(_tool_result(call, result))

    return AgentOutcome(reply=_TOO_LONG)


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
    elif call.name == "silpo_get_product_details":
        result = await method(slug=str(args.get("slug", "")), context=context)
    elif call.name in CONTEXT_TOOLS:
        result = await method(context, **args)
    else:
        result = await method(**args)

    return json.dumps(result, ensure_ascii=False, default=str)[:8000]
