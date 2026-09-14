"""The known-product resolve path: pinned to the stored id, never to the top hit."""

from komora.core.models import KnownLine
from komora.core.passes.resolve import NOT_FOUND, resolve_known
from komora.core.pipeline import build_known_cart
from tests.fakes import CONTEXT, FakeSilpo, product


def known(pid: str, name: str, *, article: int | None = None, qty: float = 1) -> KnownLine:
    return KnownLine(
        product_id=pid,
        name=name,
        quantity=qty,
        external_product_id=article,
        reason_text="Ви купуєте кожні ~7 днів, минуло 8 днів",
    )


def hit(name: str, pid: str, *, article: int | None = None, **kw: object) -> dict:
    item = product(name, 42.9, product_id=pid, **kw)  # type: ignore[arg-type]
    if article is not None:
        item["externalProductId"] = article
    return item


async def test_the_hit_is_pinned_to_the_stored_id_not_the_first_result() -> None:
    silpo = FakeSilpo(
        {"Молоко Галичина": [hit("Молоко Селянське", "other"), hit("Молоко Галичина", "mine")]}
    )
    cart, _ = await resolve_known([known("mine", "Молоко Галичина")], silpo, CONTEXT)
    assert [ln.product_id for ln in cart.lines] == ["mine"]
    assert cart.lines[0].reason_kind == "habit"
    assert cart.lines[0].description == "Молоко Галичина"


async def test_a_stored_article_number_is_the_search_term() -> None:
    silpo = FakeSilpo({"815253": [hit("Молоко Галичина", "mine", article=815253)]})
    cart, learned = await resolve_known(
        [known("mine", "Молоко Галичина", article=815253)], silpo, CONTEXT
    )
    assert silpo.search_calls == [["815253"]]
    assert [ln.product_id for ln in cart.lines] == ["mine"]
    assert learned == {}  # nothing new to learn


async def test_a_name_search_that_finds_the_id_teaches_the_article_number() -> None:
    silpo = FakeSilpo({"Кефір РадиМо": [hit("Кефір РадиМо", "k", article=742587)]})
    _, learned = await resolve_known([known("k", "Кефір РадиМо")], silpo, CONTEXT)
    assert learned == {"k": 742587}


async def test_a_product_the_search_cannot_find_is_a_named_warning() -> None:
    silpo = FakeSilpo({"Сьомга стейк": [hit("Сьомга філе", "other")]})
    cart, _ = await resolve_known([known("mine", "Сьомга стейк")], silpo, CONTEXT)
    assert cart.lines == []
    assert cart.warnings == [f"{NOT_FOUND}:Сьомга стейк"]


async def test_out_of_stock_takes_the_ordinary_substitution_path() -> None:
    gone = hit("Молоко Галичина", "mine", stock=0, available=False)
    silpo = FakeSilpo(
        {"Молоко Галичина": [gone]},
        replacements={"mine": [hit("Молоко Селянське", "sub")]},
    )
    cart, _ = await resolve_known([known("mine", "Молоко Галичина")], silpo, CONTEXT)
    (line,) = cart.lines
    assert line.product_id == "sub" and line.reason_kind == "sub"
    assert line.substituted_from == "Молоко Галичина"


async def test_out_of_stock_with_no_substitute_stays_visible_and_uncounted() -> None:
    gone = hit("Молоко Галичина", "mine", stock=0, available=False)
    silpo = FakeSilpo({"Молоко Галичина": [gone]})
    cart, _ = await resolve_known([known("mine", "Молоко Галичина", qty=2)], silpo, CONTEXT)
    (line,) = cart.lines
    assert line.unavailable and cart.total == 0


async def test_duplicate_terms_are_searched_once_and_chunked_by_thirty() -> None:
    lines = [known(f"p{i}", f"Товар {i}") for i in range(35)]
    silpo = FakeSilpo({f"Товар {i}": [hit(f"Товар {i}", f"p{i}")] for i in range(35)})
    cart, _ = await resolve_known([*lines, lines[0]], silpo, CONTEXT)
    assert [len(c) for c in silpo.search_calls] == [30, 5]
    assert len(cart.lines) == 36


async def test_build_known_cart_runs_the_rest_of_the_pipeline() -> None:
    silpo = FakeSilpo(
        {"Молоко Галичина": [hit("Молоко Галичина", "mine", old_price=52.9)]},
        coupons=[],
    )
    cart, learned = await build_known_cart(
        [known("mine", "Молоко Галичина", qty=2)], silpo, CONTEXT, budget_cap=50
    )
    assert cart.estimated_savings == 20  # (52.90 - 42.90) × 2 via apply_savings
    assert any(w.startswith("over_budget:") for w in cart.warnings)
    assert learned == {}
