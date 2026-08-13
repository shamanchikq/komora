"""«Заміни ковбаски на салямі» — which product in the cart is «ковбаски»?

The model names what to drop in the user's words; this decides what that points at.
The candidate set is **only what Komora itself synced**, so nothing the user added in
the Silpo app can be matched, however well the words fit.

Matching is lexical and deliberately strict. A false positive here deletes food from a
real cart, while a false negative leaves an extra item the user can remove in the Silpo
app in two taps — so every meaningful word of the request must land, and a request that
lands nowhere simply removes nothing.

Ukrainian inflection rules out equality: the user writes «ковбаски», the line is
«Ковбаса Глобино Салямі». Prefix matching on a shared stem handles that without a
lemmatiser, which is the same reason the search prompt asks for nominative case.
"""

import re

from komora.core.models import CartRemoval, ResolvedLine
from komora.core.passes.words import same_word

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

# Word matching lives in `passes/words.py`, shared with the category index. A fixed
# stem length was tried here and measured wrong on the commonest nouns in a grocery
# list: «колу»/«кола», «воду»/«вода», «сиру»/«сир» and «яйця»/«яйце» all failed it, so
# «прибери колу» matched nothing at all. Only «ковбаски»/«ковбаса» passed — the single
# example it had been checked against.

_STOPWORDS = frozenset(
    {
        "але",
        "будь",
        "все",
        "для",
        "його",
        "замість",
        "йому",
        "ласка",
        "мені",
        "має",
        "моє",
        "нема",
        "немає",
        "оце",
        "той",
        "теж",
        "тільки",
        "цей",
        "цю",
        "ще",
        "що",
        "як",
    }
)
"""Words that carry no product meaning. Kept short on purpose — an over-eager list
would strip a request down to nothing, and a request with no meaningful words matches
nothing at all rather than matching everything."""


def tokens(text: str) -> list[str]:
    """Meaningful lowercase words. Digits and punctuation are dropped: «2,6%» and «45%»
    describe a variant, and a removal is about the product, not the variant."""
    return [
        word
        for word in (m.group().casefold() for m in _WORD.finditer(text))
        if len(word) > 2 and word not in _STOPWORDS
    ]


def matches(request: str, line: ResolvedLine) -> bool:
    """Does `request` name this line?

    Both the query the line came from and the product Silpo returned are searched: the
    user may echo either — «прибери ковбаски» (the description) or «прибери Глобино»
    (the brand they can see in the draft).
    """
    wanted = tokens(request)
    if not wanted:
        return False
    have = tokens(f"{line.description} {line.name}")
    return all(any(same_word(word, other) for other in have) for word in wanted)


def match_removals(
    requests: list[str],
    candidates: list[ResolvedLine],
    *,
    keep: set[str] | None = None,
    present: set[str] | None = None,
) -> list[CartRemoval]:
    """Resolve described removals against the lines Komora has already synced.

    `keep` holds product ids the same basket is adding. A model that names a product in
    both `lines` and `removals` — «заміни ковбаски на ковбаски пепероні» would tempt
    one — must not have Komora add it and then delete it in the same confirmation.

    `present` holds what is in the Silpo cart right now. Komora's record of what it
    synced only says what it *put* there, and a product removed last turn still matches
    the words perfectly — so a draft would offer to remove something already gone, and
    then silently not do it, because only the confirmation sheet ever checked. Omitting
    this argument keeps the old, unchecked behaviour, which is why it is explicit at
    every call site rather than optional in spirit.

    Every matching candidate is returned, not the best one: «прибери ковбаски» with two
    sausages in the cart means both. Ordered by the candidates, so the confirmation
    sheet reads in cart order.
    """
    protected = keep or set()
    out: dict[str, CartRemoval] = {}
    for line in candidates:
        if line.product_id in protected or line.product_id in out:
            continue
        if present is not None and line.product_id not in present:
            continue
        if any(matches(request, line) for request in requests):
            out[line.product_id] = CartRemoval(product_id=line.product_id, name=line.name)
    return list(out.values())
