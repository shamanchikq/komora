"""Does the product we picked actually match what was asked for?

Nothing else in the pipeline can answer this. «основа для піци» resolved to
«Корзинка Progelcone для морозива цукрова» — an ice-cream cone — and every check we
have passed it: the product exists, it is in stock, it has a sane price, and its name
shares words with the query. Only a reader notices it is not pizza base.

So a reader is asked. **One call for the whole basket**, because the binding limit on
the free Gemini tier is requests per minute and per day, not tokens — a longer prompt
is free, a second round trip is not.

The model's answer never reaches the cart directly: it can flag a line and suggest a
better query, and the deterministic pipeline decides what to do with that. A flagged
line that cannot be re-resolved is reported as «не знайшлося», never quietly kept.
"""

import json
from collections.abc import Sequence
from typing import Any, Final

from komora.core.llm.protocol import LLMClient, Message, ToolDecl

REPORT_TOOL: Final = "report_mismatches"
DEGRADED_VERIFY: Final = "degraded:verification"

SYSTEM: Final = """\
Ти перевіряєш, чи підібрані товари відповідають тому, що просив користувач.

Перший рядок повідомлення — для чого цей кошик. Це частина перевірки: товар буває
правильної категорії, але не для цієї страви. Для піци пепероні «ковбаса сирокопчена
салямі» — ok, а «м'ясний снек до пива» чи «сиров'ялені міні-салямі фасовані» —
mismatch, бо на піцу таке не кладуть.

Далі для кожної позиції тобі дають ЗАПИТ і НАЗВУ обраного товару. Признач кожній:
  ok      — товар підходить під запит (навіть якщо це інший бренд чи розмір)
  mismatch — це не той товар: інша категорія, інше призначення, явно не те

Приклад mismatch: запит «основа для піци», обрано «Корзинка для морозива цукрова».
Приклад ok: запит «сир твердий», обрано «Сир Пирятин Король сирів твердий 50%».

Будь стриманим: познач mismatch лише тоді, коли помилка очевидна. Інший бренд,
інша жирність чи інша вага — це НЕ mismatch.

Для кожного mismatch додай better_query — 1-3 звичайні слова для нового пошуку,
у називному відмінку, без дужок.

Виклич {tool} рівно один раз.
"""

SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "description": "Одна відповідь на кожну позицію, у тому самому порядку.",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Номер позиції зі списку."},
                    "verdict": {
                        "type": "string",
                        "enum": ["ok", "mismatch"],
                        "description": "ok або mismatch.",
                    },
                    "better_query": {
                        "type": "string",
                        "description": (
                            "Лише для mismatch: 1-3 слова для нового пошуку українською."
                        ),
                    },
                },
                "required": ["index", "verdict"],
            },
        }
    },
    "required": ["results"],
}

DECLARATION: Final = ToolDecl(
    name=REPORT_TOOL,
    description="Повідомити, які позиції не відповідають запиту.",
    parameters=SCHEMA,
)


async def find_mismatches(
    llm: LLMClient, pairs: Sequence[tuple[str, str]], purpose: str = ""
) -> dict[int, str] | None:
    """Index -> a better query, for lines the model says do not match.

    `purpose` is the basket's own title — «Інгредієнти для піци пепероні». Without it
    each line is judged alone, and alone a dry-cured snack salami is a perfectly good
    answer to «ковбаса салямі»; only the basket says it is going on a pizza. It rides
    the user message rather than the system prompt so the cached prefix stays byte-
    identical across calls.

    Returns `None` when the check could not be run, which the caller reports as a
    degraded cart. An empty dict means everything passed — the two must not be
    conflated, or a broken verifier would read as a clean bill of health.
    """
    if not pairs:
        return {}

    listing = "\n".join(
        f"{i}. ЗАПИТ: «{description}» → ОБРАНО: «{name}»"
        for i, (description, name) in enumerate(pairs)
    )
    heading = f"КОШИК: «{purpose.strip()}»" if purpose.strip() else "КОШИК: не вказано"
    listing = f"{heading}\n\n{listing}"
    try:
        response = await llm.complete(
            system=SYSTEM.format(tool=REPORT_TOOL),
            messages=[Message("user", listing)],
            tools=[DECLARATION],
        )
    except Exception:
        return None

    call = next((c for c in response.tool_calls if c.name == REPORT_TOOL), None)
    if call is None:
        return None

    return _mismatches(call.args, len(pairs))


def _mismatches(args: dict[str, Any], count: int) -> dict[int, str] | None:
    """Read the verdicts defensively.

    Returns `None` when no list of verdicts could be read at all — the check ran but
    its answer is unreadable, which is a degraded run and must reach the caller as
    one, never as a clean bill of health. A malformed *entry* is different: one bad
    line of many is skipped, and the verdicts around it stand.
    """
    results = args.get("results")
    if isinstance(results, str):
        # Some providers hand back a JSON string where an array was declared.
        try:
            results = json.loads(results)
        except json.JSONDecodeError:
            return None
    if not isinstance(results, list):
        return None

    out: dict[int, str] = {}
    for entry in results:
        if not isinstance(entry, dict) or str(entry.get("verdict", "")).lower() != "mismatch":
            continue
        try:
            index = int(entry["index"])
        except KeyError, TypeError, ValueError:
            continue
        if 0 <= index < count:
            out[index] = " ".join(str(entry.get("better_query") or "").split())
    return out
