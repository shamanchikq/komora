"""Ukrainian wording rules both surfaces need and `core/` may not import from `bot/`."""


def pl(n: float, one: str, few: str, many: str) -> str:
    """Ukrainian plural: 1 позиція, 2 позиції, 5 позицій, 11 позицій, 21 позиція."""
    count = abs(int(n))
    if count % 100 in range(11, 15):
        return many
    last = count % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


def days(n: int) -> str:
    return f"{n} {pl(n, 'день', 'дні', 'днів')}"
