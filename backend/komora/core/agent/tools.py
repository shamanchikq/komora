"""What the agent is allowed to see and call.

The split is read versus write, not feature by feature. Every *read* tool is open to
the model, so an off-script question ("які грузинські вина є до 500 ₴?") just works.
No *write* tool is exposed at all — cart mutations go through the deterministic
pipeline and an explicit user confirmation.

`propose_basket` is local: it is how the model hands a basket back, and it never
reaches Silpo.
"""

import re
from collections.abc import Collection, Sequence
from typing import Any, Final, Protocol

from komora.core.agent.recap import SYNCED_TAG
from komora.core.llm.protocol import ToolDecl
from komora.core.mcp.protocol import SilpoClient

PROPOSE_BASKET: Final = "propose_basket"

READ_TOOLS: Final[dict[str, str]] = {
    "silpo_find_products_batch": "find_products_batch",
    "silpo_get_products": "get_products",
    "silpo_get_product_details": "get_product_details",
    "silpo_get_promotions": "get_promotions",
    "silpo_get_my_coupons": "get_my_coupons",
    "silpo_get_categories": "get_categories",
    # Plan 4: three more reads, so «що з акцій?» and «щось схоже на …» can be answered.
    # Still a hand-written allowlist — a server-supplied `readOnlyHint` may check this
    # list (`test_agent_loop`), it never extends it.
    "silpo_get_my_promos": "get_my_promos",
    "silpo_get_product_sets": "get_product_sets",
    "silpo_get_similar_products": "get_similar_products",
}
"""Tool name -> the `SilpoClient` method that serves it. Nothing here mutates."""

CONTEXT_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "silpo_find_products_batch",
        "silpo_get_products",
        "silpo_get_product_details",
        "silpo_get_promotions",
        "silpo_get_categories",
        "silpo_get_product_sets",
        "silpo_get_similar_products",
    }
)
"""Read tools whose published schema requires branch or delivery context.

Every one of them lists at least `branchId` as required, so a call the loop dispatched
without it would fail validation rather than return a result. `get_similar_products`
requires the full slot too since 2026-09-14 (reference §10.1).
"""

MAX_LISTED_PRODUCTS: Final = 20
"""How many products of a browse the model is shown.

A page of a hundred is ~55 000 characters (reference §10.7), and `_dispatch` used to
cut the JSON at 8 000 — mid-object, so the model read a truncated string and guessed
at the rest. Clipping the *list* keeps every product it does see whole, and the
payload says how many were left out.
"""

INJECTED_PARAMS: Final[frozenset[str]] = frozenset(
    {"branchId", "deliveryType", "timeslotStart", "timeslotEnd"}
)
"""Supplied by the loop from the user's cart, and hidden from the model.

Shown the real schema, a model reasonably stalls asking which branch and delivery slot
to use — it has no way to know. Observed live: with these visible the model asked for
store details instead of answering. The loop knows them, so it fills them in.
"""

PROPOSE_BASKET_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Коротка назва кошика українською, напр. «Звичайний кошик».",
        },
        "lines": {
            "type": "array",
            "description": "Позиції кошика. Щонайменше одна.",
            "items": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "Пошуковий запит українською: 2-3 звичайні слова — «молоко 2,6%», "
                            "«ковбаса салямі», «сир твердий». БЕЗ дужок, без «наприклад», "
                            "без «або» — з ними пошук не знаходить нічого. Один товар на "
                            "позицію. Не назва конкретного товару Сільпо. "
                            "Обовʼязкове, не може бути null."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "description": (
                            "Категорія Сільпо українською, якщо знаєш точну — «Курячі яйця», "
                            "«Тверді сири», «Ковбаси». Це усуває плутанину, якої пошук не "
                            "уникає (курячі яйця vs яйця цесарки). Пропусти, якщо не "
                            "впевнений — краще без категорії, ніж не та."
                        ),
                    },
                    "quantity": {
                        "type": "number",
                        "description": (
                            "Скільки одиниць. Число, не текст. 1, якщо користувач не "
                            "попросив більше — не вгадуй запас."
                        ),
                    },
                    "amount": {
                        "type": "object",
                        "description": (
                            "Скільки ПРОДУКТУ потрібно, коли це відомо з рецепта чи з "
                            "кількості гостей — «1,5 кг», «3 л», «10 шт». Система сама "
                            "перерахує в упаковки за розміром пакування Сільпо. Пропусти, "
                            "якщо кількість не має значення."
                        ),
                        "properties": {
                            "value": {"type": "number", "description": "Число, напр. 1.5."},
                            "unit": {
                                "type": "string",
                                "description": "Одиниця українською: «кг», «г», «л», «мл», «шт».",
                            },
                        },
                        "required": ["value", "unit"],
                    },
                    "reason_text": {
                        "type": "string",
                        "description": (
                            "Чому ця позиція тут, українською. Показується користувачу "
                            "під назвою товару."
                        ),
                    },
                    "optional": {
                        "type": "boolean",
                        "description": "true, якщо позицію можна прибрати за потреби зекономити.",
                    },
                },
                "required": ["description", "quantity", "reason_text"],
            },
        },
        "menu": {
            "type": "array",
            "description": (
                "Лише для плану харчування: страви по днях українською — "
                "[{day: «понеділок», dish: «борщ»}]. Показується над кошиком. "
                "Порожній масив, якщо це не план."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "string", "description": "День або прийом їжі, українською."},
                    "dish": {"type": "string", "description": "Назва страви українською."},
                },
                "required": ["day", "dish"],
            },
        },
        "guests": {
            "type": "integer",
            "description": (
                "Лише для події («на 10 людей»): кількість людей. Кількості в lines "
                "уже мають бути розраховані на них."
            ),
        },
        "removals": {
            "type": "array",
            "description": (
                "Товари, які треба ПРИБРАТИ з кошика Сільпо, звичайними словами — "
                "«ковбаски пепероні», «молоко». Заповнюй лише тоді, коли кошик уже "
                f"надіслано (в історії є «{SYNCED_TAG}») і користувач "
                "просить щось замінити або прибрати. Комора прибере тільки те, що "
                "додала сама, і тільки після підтвердження користувача. "
                "Порожній масив, якщо прибирати нічого."
            ),
            "items": {"type": "string"},
        },
    },
    "required": ["title", "lines"],
}
"""Hand-written and flat, deliberately.

`DraftBasket.model_json_schema()` would emit `$defs`/`$ref` for the nested line model:
Gemini's converter must inline those, and Gemma degrades on them — Google's own cookbook
warns against auto-generated schemas for nested parameters. Every field states its
language, because parameter-value language leakage is the dominant multilingual
tool-calling failure.
"""


def strip_injected(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove the parameters the loop supplies, so the model never sees them."""
    properties = {
        name: value
        for name, value in (schema.get("properties") or {}).items()
        if name not in INJECTED_PARAMS
    }
    required = [name for name in (schema.get("required") or []) if name not in INJECTED_PARAMS]
    return {**schema, "properties": properties, "required": required}


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_PARAGRAPH = re.compile(r"\n\s*\n")
_HEADING = re.compile(r"^([A-Z][A-Z0-9 /&()-]{2,40}):")

MAX_DESCRIPTION: Final = 1600
"""Long enough for the paragraphs Komora wants the model to have (article search,
package size, weighted units); short enough that nine tools stay a stable, cacheable
prefix. Cut at a paragraph, then at a sentence — never mid-word."""

DROPPED_HEADINGS: Final[frozenset[str]] = frozenset({"BUDGET"})
"""Paragraphs Silpo addresses to a generic agent and Komora's own prompt contradicts.

`find_products_batch` carries «BUDGET: If user mentions a budget, ALWAYS fill the
cart as close to the budget limit as possible». Komora's budget pass does the
opposite — it flags an overage and never adds a thing — and until 2026-09-14 the only
reason the model never read that sentence was a 400-character cut that happened to
fall before it. An accident is not a policy; this is.
"""

DROPPED_PHRASES: Final[tuple[str, ...]] = (
    "fill the cart",
    "budget limit",
    "as close to the budget",
    "maximize the total spend",
)
"""The same instruction, wherever it appears without its heading. Both lists are
checked in `test_agent_loop`: a kept description that gains one of these fails."""


def _wanted(paragraph: str) -> bool:
    heading = _HEADING.match(paragraph)
    if heading and heading.group(1).strip() in DROPPED_HEADINGS:
        return False
    lowered = paragraph.casefold()
    return not any(phrase in lowered for phrase in DROPPED_PHRASES)


def _without_unreachable(paragraph: str, unreachable: Collection[str]) -> str:
    kept = [
        sentence
        for sentence in _SENTENCE.split(paragraph)
        if not any(name in sentence for name in unreachable)
    ]
    return " ".join(s.strip() for s in kept if s.strip())


def describe(tool: dict[str, Any], unreachable: Collection[str]) -> str:
    """The description the model reads: Silpo's own, minus advice it cannot act on.

    Two kinds of advice are removed. Sentences that name a tool the model does not
    have — three read tools say to fetch branch and timeslot from
    `silpo_get_shopping_cart_by_id`, in two wordings, and those parameters are hidden
    by `strip_injected` anyway; left in, the model is told to call a tool it cannot
    reach to fill a field it cannot see. And whole paragraphs Silpo addresses to a
    generic agent that Komora's prompt contradicts (`DROPPED_HEADINGS`,
    `DROPPED_PHRASES`) — read live 2026-09-14, the descriptions are structured as
    `HEADING: text` paragraphs and one of them says to fill the cart to the budget.

    What survives is kept in Silpo's order, first paragraph first, up to
    `MAX_DESCRIPTION` — cut at a paragraph boundary, then at a sentence. The old
    400-character cut hid the article-search and package-size paragraphs the resolve
    pass now depends on the model knowing about (reference §10.6).
    """
    text = (tool.get("description") or "").strip()
    paragraphs = [
        cleaned
        for paragraph in _PARAGRAPH.split(text)
        if paragraph.strip() and _wanted(paragraph.strip())
        for cleaned in (_without_unreachable(paragraph.strip(), unreachable),)
        if cleaned
    ]
    out = ""
    for paragraph in paragraphs:
        candidate = f"{out} {paragraph}".strip()
        if len(candidate) <= MAX_DESCRIPTION:
            out = candidate
            continue
        # The paragraph that overflows is cut at a sentence, and nothing follows it.
        for sentence in _SENTENCE.split(paragraph):
            trial = f"{out} {sentence.strip()}".strip()
            if len(trial) > MAX_DESCRIPTION:
                break
            out = trial
        break
    return out


def build_tool_decls(captured_tools: list[dict[str, Any]]) -> list[ToolDecl]:
    """Build the declarations offered to the model from the captured MCP schemas."""
    unreachable = {t["name"] for t in captured_tools} - set(READ_TOOLS)
    decls = [
        ToolDecl(
            name=tool["name"],
            description=describe(tool, unreachable),
            parameters=strip_injected(tool.get("inputSchema") or {}),
        )
        for tool in captured_tools
        if tool["name"] in READ_TOOLS
    ]
    decls.append(
        ToolDecl(
            name=PROPOSE_BASKET,
            description=(
                "Запропонувати кошик користувачу. Виклич це, щойно зрозумів, що потрібно "
                "купити. Кожна позиція мусить мати причину українською."
            ),
            parameters=PROPOSE_BASKET_SCHEMA,
        )
    )
    return decls


class ToolSource(Protocol):
    """Supplies the declarations for a turn, given a live Silpo session."""

    async def __call__(self, mcp: SilpoClient) -> Sequence[ToolDecl]: ...


class CachedTools:
    """Reads the declarations off the live server once per process.

    They can only be fetched through an authenticated session, so this happens on the
    first user turn rather than at startup. Caching also matters for cost: an identical
    tool prefix on every request is what lets implicit context caching hit.
    """

    def __init__(self) -> None:
        self._decls: tuple[ToolDecl, ...] | None = None

    async def __call__(self, mcp: SilpoClient) -> Sequence[ToolDecl]:
        if self._decls is None:
            self._decls = tuple(build_tool_decls(await mcp.list_tools()))
        return self._decls
