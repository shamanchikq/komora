"""What the agent is allowed to see and call.

The split is read versus write, not feature by feature. Every *read* tool is open to
the model, so an off-script question ("які грузинські вина є до 500 ₴?") just works.
No *write* tool is exposed at all — cart mutations go through the deterministic
pipeline and an explicit user confirmation.

`propose_basket` is local: it is how the model hands a basket back, and it never
reaches Silpo.
"""

from typing import Any, Final

from komora.core.llm.protocol import ToolDecl

PROPOSE_BASKET: Final = "propose_basket"

READ_TOOLS: Final[dict[str, str]] = {
    "silpo_find_products_batch": "find_products_batch",
    "silpo_get_products": "get_products",
    "silpo_get_product_details": "get_product_details",
    "silpo_get_promotions": "get_promotions",
    "silpo_get_my_coupons": "get_my_coupons",
    "silpo_get_categories": "get_categories",
}
"""Tool name -> the `SilpoClient` method that serves it. Nothing here mutates."""

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
                            "Що купити, звичайними словами українською — напр. «молоко 2,6%». "
                            "НЕ назва конкретного товару Сільпо. Обовʼязкове, не може бути null."
                        ),
                    },
                    "quantity": {
                        "type": "number",
                        "description": "Скільки одиниць. Число, не текст. За замовчуванням 1.",
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


def build_tool_decls(captured_tools: list[dict[str, Any]]) -> list[ToolDecl]:
    """Build the declarations offered to the model from the captured MCP schemas."""
    decls = [
        ToolDecl(
            name=tool["name"],
            description=(tool.get("description") or "").strip()[:400],
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
