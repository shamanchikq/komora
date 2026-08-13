"""Is this the same Ukrainian word, in a different case?

Two passes need the answer and both got it wrong independently. `removals` asked whether
«прибери колу» names the «Кола» in the cart; `categories` asked whether the model's
«Кола» names the «Кола» shelf. Each rolled its own rule, each was tuned on the one
example its author happened to try, and each failed on the commonest nouns in a grocery
catalogue.

Ukrainian inflects, so equality is useless: «колу»/«кола», «сиру»/«сир», «яйця»/«яйце».
Substring matching is worse than useless — it reads a *morpheme* boundary where there is
none, which is how `CategoryIndex` resolved «Кола» to **колаген** and «Вино» to
**виноград**, handing a correct category answer a wrong shelf.

The rule is a ratio, not a fixed stem length. Two words are the same word when their
shared prefix covers most of the longer one: an inflectional ending is a character or
two, while a derivational suffix that changes the meaning is longer. A fixed length
cannot separate «ковбаски»/«ковбаса» (same word, two characters apart) from
«сирок»/«сир» (curd snack vs cheese, also two characters apart) — the ratio can,
because the words are different lengths.

Nothing here is morphology. It is a cheap approximation that was measured against the
cases this project actually hits, and it will misjudge words nobody has tried yet.
"""

AGREEMENT = 0.7
"""Share of the longer word the two must have in common.

Measured against the real category tree and real removal requests:

    кола/колаген   4/7 = 0.57  different   (the bug this exists to stop)
    вино/виноград  4/8 = 0.50  different
    сир/сирний     3/6 = 0.50  different
    сирок/сир      3/5 = 0.60  different
    колу/кола      3/4 = 0.75  same
    сиру/сир       3/4 = 0.75  same
    яйця/яйце      3/4 = 0.75  same
    ковбаски/ковбаса 6/8 = 0.75  same
    тверді/твердий 5/7 = 0.71  same
"""

MIN_SHARED = 3
"""Below this the ratio stops meaning anything: two-letter prepositions agree with
everything that starts the same way."""


def shared_prefix(first: str, second: str) -> int:
    count = 0
    for a, b in zip(first, second, strict=False):
        if a != b:
            break
        count += 1
    return count


def same_word(first: str, second: str) -> bool:
    """Do these differ only by an ending?"""
    if first == second:
        return True
    if not first or not second:
        return False
    shared = shared_prefix(first, second)
    if shared < MIN_SHARED:
        return False
    return shared / max(len(first), len(second)) >= AGREEMENT
