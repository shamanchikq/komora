"""Empirical viability probe: can a local model drive Komora's agent loop?

Sends a realistic Komora-shaped request — ~20 read-only tool declarations plus a
nested `propose_basket` schema, prompted in Ukrainian — and checks whether the model
(a) picks the right tool, (b) emits valid arguments, (c) fills the nested structure.
"""

import asyncio
import json
import sys
import time

import httpx

OLLAMA = "http://localhost:11434"

# Mirrors the M1 read-tool allowlist plus filler, to reproduce real selection pressure.
READ_TOOLS = [
    (
        "silpo_find_products_batch",
        "Пошук кількох товарів одночасно (до 30 запитів).",
        {"queries": {"type": "array", "items": {"type": "string"}}},
    ),
    (
        "silpo_get_products",
        "Список товарів за категорією, промо або пошуковим запитом.",
        {
            "query": {"type": "string"},
            "categoryId": {"type": "string"},
            "maxPrice": {"type": "number"},
        },
    ),
    (
        "silpo_get_product_details",
        "Повна картка товару: склад, харчова цінність, наявність.",
        {"slug": {"type": "string"}},
    ),
    (
        "silpo_get_promotions",
        "Активні акції у вибраному магазині.",
        {"branchId": {"type": "string"}},
    ),
    ("silpo_get_my_coupons", "Персональні купони користувача.", {}),
    ("silpo_get_categories", "Плоский список категорій.", {}),
    (
        "silpo_get_replacements",
        "Заміни для товару, якого немає в наявності.",
        {"slug": {"type": "string"}},
    ),
    ("silpo_get_my_favorites", "Збережені товари користувача.", {}),
    ("silpo_list_branches", "Магазини мережі з фільтрами.", {"city": {"type": "string"}}),
    ("silpo_get_time_slots", "Слоти доставки для магазину.", {"branchId": {"type": "string"}}),
    ("silpo_get_loyalty_info", "Статус Власного Рахунку і баланс балабонусів.", {}),
    ("silpo_get_my_profile", "Імʼя, телефон, email, дата народження.", {}),
    ("silpo_get_my_family", "Члени родини, вік дітей, домашні тварини.", {}),
    ("silpo_get_my_food_restrictions", "Дієтичні обмеження користувача.", {}),
    ("silpo_get_my_online_orders", "Історія онлайн-замовлень.", {"limit": {"type": "integer"}}),
    (
        "silpo_get_my_offline_orders",
        "Чеки з магазинів і нараховані бонуси.",
        {"limit": {"type": "integer"}},
    ),
    ("silpo_get_categories_tree", "Ієрархія категорій.", {}),
    ("silpo_get_popular_categories", "Популярні категорії.", {}),
    ("silpo_get_product_sets", "Кураторські добірки товарів.", {}),
    ("silpo_get_my_promos", "Персональні пропозиції.", {}),
]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(props)[:1] if props else [],
            },
        },
    }
    for name, desc, props in READ_TOOLS
]

TOOLS.append(
    {
        "type": "function",
        "function": {
            "name": "propose_basket",
            "description": (
                "Запропонувати користувачу кошик. Викликай ЦЕ, коли зрозумів, що потрібно "
                "купити. Кожна позиція мусить мати причину українською."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Назва кошика"},
                    "lines": {
                        "type": "array",
                        "description": "Позиції кошика",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string", "description": "Що купити"},
                                "quantity": {"type": "number"},
                                "reason_text": {"type": "string", "description": "Чому це тут"},
                            },
                            "required": ["description", "quantity", "reason_text"],
                        },
                    },
                },
                "required": ["title", "lines"],
            },
        },
    }
)

SYSTEM = (
    "Ти — Комора, помічник для покупок у «Сільпо». Відповідай українською. "
    "Коли користувач каже, що йому потрібно купити — виклич propose_basket. "
    "Для запитів про наявність чи ціни конкретних товарів — використовуй пошукові інструменти. "
    "Кожна позиція кошика мусить мати причину."
)

CASES = [
    ("basket", "Купи молоко, хліб і щось до чаю", "propose_basket"),
    (
        "search",
        "Яке грузинське вино є до 500 гривень?",
        ("silpo_get_products", "silpo_find_products_batch"),
    ),
]


async def probe(client: httpx.AsyncClient, model: str, case: str, prompt: str, expect) -> dict:
    t0 = time.monotonic()
    try:
        r = await client.post(
            f"{OLLAMA}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "tools": TOOLS,
                "stream": False,
                "think": False,
                "options": {"num_ctx": 16384},
            },
            timeout=600.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"model": model, "case": case, "error": f"{type(e).__name__}: {e}"}

    msg = data.get("message", {})
    calls = msg.get("tool_calls") or []
    elapsed = round(time.monotonic() - t0, 1)
    expected = (expect,) if isinstance(expect, str) else expect

    out: dict = {
        "model": model,
        "case": case,
        "secs": elapsed,
        "n_calls": len(calls),
        "called": [c.get("function", {}).get("name") for c in calls],
        "correct_tool": bool(calls) and calls[0].get("function", {}).get("name") in expected,
        "text_if_no_call": (msg.get("content") or "")[:160] if not calls else "",
    }

    if calls and case == "basket":
        args = calls[0].get("function", {}).get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        lines = args.get("lines") or []
        out["n_lines"] = len(lines)
        out["schema_ok"] = bool(lines) and all(
            isinstance(ln, dict) and {"description", "quantity", "reason_text"} <= set(ln)
            for ln in lines
        )
        cyr = sum(
            1
            for ln in lines
            if isinstance(ln, dict)
            and any("Ѐ" <= ch <= "ӿ" for ch in str(ln.get("reason_text", "")))
        )
        out["ukrainian_reasons"] = f"{cyr}/{len(lines)}"
        out["sample"] = lines[0] if lines else None
    return out


async def main() -> None:
    models = sys.argv[1:]
    async with httpx.AsyncClient() as client:
        if not models:
            tags = (await client.get(f"{OLLAMA}/api/tags", timeout=10)).json()
            print("AVAILABLE:", [m["name"] for m in tags.get("models", [])])
            return
        for model in models:
            for case, prompt, expect in CASES:
                res = await probe(client, model, case, prompt, expect)
                print(json.dumps(res, ensure_ascii=False))


asyncio.run(main())
