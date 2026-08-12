"""Measure two models against Komora's real prompt, real tools and real failure cases.

Every model claim in this project has to survive a repeat run. An earlier prompt
comparison was written up from one run per cell and a second run inverted almost every
cell — so this takes `--repeats` and reports a rate, never a verdict from one sample.

What makes it worth trusting: it imports the **shipped** `SYSTEM_PROMPT` and builds the
**shipped** tool declarations from the captured `tools.json`. Nothing here reimplements
the agent surface, so a pass means the real thing passed. Silpo is never contacted —
the tool schemas come from the fixture — so this costs Gemini requests and nothing else.

The scenarios are the failures this project actually had, not synthetic ones:

  pizza        overordering and vague descriptions (the 1 kg parmesan, «Ковбаса
               (наприклад, салямі)» matching nothing)
  edit         `removals` after a sync — shipped 2026-08-12 and never measured on any
               model. If a model will not emit it, replacing a synced product silently
               does nothing, which is the bug it was written for.
  question     the free-form path must NOT propose a basket
  verify       the verification pass must catch a beer-snack salami in a pizza basket

USAGE
    uv run python scripts/compare_models.py
    uv run python scripts/compare_models.py --repeats 5
    uv run python scripts/compare_models.py --models gemini/gemini-3.1-flash-lite \
        gemini/gemini-3.5-flash-lite --only edit

COST
    len(models) x len(scenarios) x repeats requests. The default is 2 x 4 x 3 = 24,
    against a free-tier allowance of 500 per model per day. Requests are paced for the
    15/minute ceiling.
"""

import argparse
import asyncio
import json
import pathlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from _report import INFO, note
from dotenv import load_dotenv

from komora.config import Settings
from komora.core.agent.prompts import SYSTEM_PROMPT
from komora.core.agent.recap import DRAFT_TAG, SYNCED_TAG
from komora.core.agent.tools import PROPOSE_BASKET, build_tool_decls
from komora.core.llm.factory import make_llm
from komora.core.llm.protocol import LLMResponse, Message
from komora.core.models import DraftBasket
from komora.core.passes.verify import find_mismatches

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
TOOLS_FIXTURE = ROOT / "tests" / "fixtures" / "mcp" / "tools.json"

DEFAULT_MODELS = ["gemini/gemini-3.1-flash-lite", "gemini/gemini-3.5-flash-lite"]

RPM_PACING = 4.5
"""Seconds between requests. The free tier allows 15/minute per model and this runs
one model at a time, so 4.5s leaves headroom for a retry without tripping a 429."""


def banner_line(title: str) -> None:
    print(f"\n{'-' * 70}\n{title}\n{'-' * 70}")


@dataclass
class Outcome:
    passed: bool
    detail: str


def _basket(response: LLMResponse) -> DraftBasket | None:
    call = next((c for c in response.tool_calls if c.name == PROPOSE_BASKET), None)
    if call is None:
        return None
    try:
        return DraftBasket.model_validate({**call.args, "intent": "stated"})
    except Exception:
        return None


# --- scenarios -------------------------------------------------------------------

PIZZA_MESSAGE = "Додай інгредієнти для піци пепероні"

SYNCED_HISTORY = (
    f"{DRAFT_TAG} Інгредієнти для піци пепероні\n"
    "1. борошно для піци → Борошно пшеничне La Farina di Cuneo для піци × 1\n"
    "2. сир моцарела → Сир Яготинська Моцарела міні 45% × 1\n"
    "3. томатний соус → Соус томатний Mutti з пармезаном × 1\n"
    "4. ковбаса пепероні → Ковбаски Глобино Салямі Пепероні с/к × 1\n"
    f"{SYNCED_TAG} Борошно пшеничне La Farina di Cuneo для піци; "
    "Сир Яготинська Моцарела міні 45%; Соус томатний Mutti з пармезаном; "
    "Ковбаски Глобино Салямі Пепероні с/к"
)

BAD_DESCRIPTION_MARKERS = ("(", "наприклад", " або ")
"""Measured live: «Ковбаса (наприклад, салямі або варена)» returns 0 products while
«ковбаса» returns 30. A description carrying one of these is a line that will not
resolve."""


def score_pizza(response: LLMResponse) -> Outcome:
    basket = _basket(response)
    if basket is None:
        return Outcome(False, "no valid propose_basket")

    problems = []
    vague = [
        ln.description
        for ln in basket.lines
        if any(m in ln.description.casefold() for m in BAD_DESCRIPTION_MARKERS)
    ]
    if vague:
        problems.append(f"vague: {vague}")

    greedy = [f"{ln.description}={ln.quantity}" for ln in basket.lines if ln.quantity > 2]
    if greedy:
        problems.append(f"qty>2: {greedy}")

    if not 3 <= len(basket.lines) <= 7:
        problems.append(f"{len(basket.lines)} lines")

    categorised = sum(1 for ln in basket.lines if ln.category)
    detail = f"{len(basket.lines)} lines, {categorised} categorised"
    return Outcome(not problems, detail if not problems else "; ".join(problems))


def score_edit(response: LLMResponse) -> Outcome:
    """The behaviour shipped 2026-08-12: an edit after a sync must name a removal.

    Without it the replaced product stays in the real cart next to its replacement,
    which is exactly the bug the field exists for.
    """
    basket = _basket(response)
    if basket is None:
        return Outcome(False, "no valid propose_basket")
    if not basket.removals:
        return Outcome(False, f"no removals; lines={[ln.description for ln in basket.lines]}")

    names = " ".join(basket.removals).casefold()
    if "ковбас" not in names and "пепероні" not in names:
        return Outcome(False, f"removals name the wrong thing: {basket.removals}")

    # Re-listing every unchanged line is not fatal — re-adding sets quantity rather
    # than incrementing — but it is the drift that changed a sauce nobody mentioned.
    noise = ""
    if len(basket.lines) > 2:
        noise = f" (also re-proposed {len(basket.lines)} lines)"
    return Outcome(True, f"removals={basket.removals}{noise}")


def score_question(response: LLMResponse) -> Outcome:
    """The free-form path. Proposing a basket here is the failure."""
    if any(c.name == PROPOSE_BASKET for c in response.tool_calls):
        return Outcome(False, "proposed a basket instead of answering")
    if response.tool_calls:
        return Outcome(True, f"searched: {response.tool_calls[0].name}")
    if response.text and response.text.strip():
        return Outcome(True, "answered as text (did not search)")
    return Outcome(False, "empty response")


SCENARIOS: dict[str, dict[str, Any]] = {
    "pizza": {
        "history": (),
        "message": PIZZA_MESSAGE,
        "score": score_pizza,
        "why": "overordering and unresolvable descriptions",
    },
    "edit": {
        "history": (Message("user", PIZZA_MESSAGE), Message("assistant", SYNCED_HISTORY)),
        "message": "Заміни ковбаски на ковбасу салямі",
        "score": score_edit,
        "why": "removals after a sync — never measured before",
    },
    "question": {
        "history": (),
        "message": "яке грузинське вино є до 500 ₴?",
        "score": score_question,
        "why": "must not propose a basket",
    },
}

VERIFY_CASE = {
    "purpose": "Інгредієнти для піци пепероні",
    "pairs": [
        ("борошно для піци", "Борошно пшеничне La Farina di Cuneo для піци"),
        ("сир моцарела", "Сир Яготинська Моцарела міні 45%"),
        ("томатний соус", "Соус томатний Mutti з пармезаном"),
        (
            "ковбаса салямі",
            "Ковбаски Лавка Традицій Світ м'яса Міні Салямі зі свинини с/в фасовані",
        ),
    ],
    "must_flag": 3,
    "must_not_flag": (0, 1, 2),
}
"""The real basket from the 2026-08-12 run. Line 3 is a dry-cured snack salami that
reads as a fine answer to «ковбаса салямі» until you know it is going on a pizza."""


async def run_scenario(llm: Any, name: str, spec: dict[str, Any], tools: list) -> Outcome:
    messages = [*spec["history"], Message("user", spec["message"])]
    try:
        response = await llm.complete(system=SYSTEM_PROMPT, messages=messages, tools=tools)
    except Exception as exc:
        return Outcome(False, f"{type(exc).__name__}: {exc}"[:120])
    return spec["score"](response)


async def run_verify(llm: Any) -> Outcome:
    """Exercises the shipped verification pass, including its basket-purpose context."""
    try:
        found = await find_mismatches(llm, VERIFY_CASE["pairs"], VERIFY_CASE["purpose"])
    except Exception as exc:
        return Outcome(False, f"{type(exc).__name__}: {exc}"[:120])
    if found is None:
        return Outcome(False, "verifier could not run (reported as degraded)")

    flagged = set(found)
    if VERIFY_CASE["must_flag"] not in flagged:
        return Outcome(False, f"missed the snack salami; flagged={sorted(flagged)}")
    false_positives = flagged & set(VERIFY_CASE["must_not_flag"])
    if false_positives:
        return Outcome(False, f"false positives on {sorted(false_positives)}")
    return Outcome(True, f"caught it; better_query={found[VERIFY_CASE['must_flag']]!r}")


async def main(args: argparse.Namespace) -> int:
    load_dotenv(ENV_FILE)
    if not TOOLS_FIXTURE.exists():
        print(f"missing {TOOLS_FIXTURE} — run scripts/verify_mcp.py first")
        return 2

    settings = Settings(
        telegram_bot_token="unused",
        llm_lite=args.models[0],
        llm_full=args.models[0],
        _env_file=str(ENV_FILE),
    )
    captured = json.loads(TOOLS_FIXTURE.read_text(encoding="utf-8"))
    tools = build_tool_decls(captured if isinstance(captured, list) else captured["tools"])

    selected = {k: v for k, v in SCENARIOS.items() if not args.only or k in args.only}
    do_verify = not args.only or "verify" in args.only
    total = len(args.models) * (len(selected) + int(do_verify)) * args.repeats
    note(
        f"{len(tools)} tool declarations from the fixture · {total} requests"
        f" · ~{total * RPM_PACING / 60:.1f} min"
    )

    results: dict[tuple[str, str], list[Outcome]] = defaultdict(list)
    for ref in args.models:
        banner_line(f"{ref}")
        llm = make_llm(ref, settings)
        for run in range(args.repeats):
            for name, spec in selected.items():
                outcome = await run_scenario(llm, name, spec, tools)
                results[(ref, name)].append(outcome)
                mark = "PASS" if outcome.passed else "FAIL"
                print(f"  [{run + 1}/{args.repeats}] {name:9} {mark}  {outcome.detail}")
                await asyncio.sleep(RPM_PACING)
            if do_verify:
                outcome = await run_verify(llm)
                results[(ref, "verify")].append(outcome)
                mark = "PASS" if outcome.passed else "FAIL"
                print(f"  [{run + 1}/{args.repeats}] {'verify':9} {mark}  {outcome.detail}")
                await asyncio.sleep(RPM_PACING)

    banner_line("Summary — pass rate over repeats")
    names = [*selected, *(["verify"] if do_verify else [])]
    width = max(len(m) for m in args.models) + 2
    print(f"{'model':<{width}}" + "".join(f"{n:>12}" for n in names))
    for ref in args.models:
        row = f"{ref:<{width}}"
        for name in names:
            outcomes = results[(ref, name)]
            hits = sum(1 for o in outcomes if o.passed)
            row += f"{f'{hits}/{len(outcomes)}':>12}"
        print(row)

    print(
        f"\n{INFO} A rate is not a verdict. With --repeats {args.repeats} a 3/3 and a 2/3 "
        "are not distinguishable;\n   raise repeats before concluding anything from a "
        "single column."
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", nargs="*", help=f"subset of: {', '.join([*SCENARIOS, 'verify'])}")
    sys.exit(asyncio.run(main(parser.parse_args())))
