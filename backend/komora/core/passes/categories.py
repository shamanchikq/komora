"""Silpo's own taxonomy, used to disambiguate what free-text search cannot.

Search conflates things the catalogue itself separates. Asked for «яйця» Silpo returns
«Яйця цесарки» — guinea fowl at 257,40 ₴ — ahead of hen's eggs at 59,39 ₴, because as a
string it is a perfectly good match. The category tree already knows the difference:
«Курячі яйця», «Перепелині яйця» and «Фермерські яйця» are three separate categories.

Browsing one returns products in the **same shape as a search result** — `id`,
`companyId`, `price`, `stock` — so nothing downstream has to care where a candidate
came from.

**`category` takes the slug.** Passing the category's `id`, which sits right there on
the same object, returns "No products found" and no error at all.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from komora.core.mcp.protocol import SilpoClient
    from komora.core.models import SearchContext

MIN_TITLE_WORD = 3
"""Below this a word matches half the tree and distinguishes nothing."""

PAGE = 1000
"""`limit` is capped at 1000 by the schema, and the tree is larger than that."""

MAX_PAGES = 10
"""A backstop: a server that never advances `offset` must not loop forever."""


async def fetch_categories(mcp: SilpoClient, context: SearchContext) -> list[dict[str, Any]]:
    """Every category, following `meta.total` past the 1000-row page limit.

    The first capture asked for `limit=1000` and stopped, which read as the whole tree
    because 1000 is a suspiciously round and complete-looking number. It was 1000 of
    1010, and the ten it dropped were **top-level** categories — leaving 71 of their
    children with a `parentId` pointing at nothing, and «Вода», «Побутова хімія» and
    «Особиста гігієна» unmatchable.
    """
    collected: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        payload = await mcp.get_categories(context, limit=PAGE, offset=page * PAGE)
        batch = _listed(payload)
        collected.extend(batch)

        total = (payload.get("meta") or {}).get("total") if isinstance(payload, dict) else None
        if not batch or not isinstance(total, int) or len(collected) >= total:
            break
    return collected


class CategoryIndex:
    """Ukrainian category titles to slugs, built once per process.

    1000 categories arrive in a single call and never change during a conversation,
    so this is cached by `resolve` rather than re-fetched per basket.
    """

    def __init__(self, payload: Any) -> None:
        self._by_title: dict[str, str] = {}
        for entry in _listed(payload):
            title = str(entry.get("title") or "").strip()
            slug = str(entry.get("slug") or "").strip()
            if title and slug:
                # First wins: the tree is ordered general-to-specific, and «Яйця»
                # is a safer answer than «Яйця інших птахів» for an ambiguous ask.
                self._by_title.setdefault(title.casefold(), slug)

    def __len__(self) -> int:
        return len(self._by_title)

    def slug_for(self, category: str | None) -> str | None:
        """Best slug for a category the model named, or None to fall back to search.

        Deliberately conservative. A wrong category is worse than no category: it
        silently narrows the catalogue to the wrong shelf, and unlike a bad search
        result there is nothing in the product name to give it away.
        """
        wanted = (category or "").strip().casefold()
        if not wanted:
            return None
        if wanted in self._by_title:
            return self._by_title[wanted]

        words = [w for w in wanted.split() if len(w) >= MIN_TITLE_WORD]
        if not words:
            return None

        # Every word of the request present in the title — «курячі яйця» matches
        # «Курячі яйця», and a bare «яйця» does not match «Перепелині яйця» alone
        # because the shortest title wins below.
        matches = [
            (title, slug)
            for title, slug in self._by_title.items()
            if all(word in title for word in words)
        ]
        if not matches:
            return None
        return min(matches, key=lambda pair: len(pair[0]))[1]


def _listed(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        entries = payload.get("categories")
        if isinstance(entries, list):
            return [e for e in entries if isinstance(e, dict)]
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    return []
