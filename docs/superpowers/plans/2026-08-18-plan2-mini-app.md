# Plan 2 — Mini App (M2)

**Status:** not started. Written 2026-08-18, immediately after the Plan 1 manual
checklist reached 8/8 against live Silpo.

Scope from the design spec §14 (kept outside this repo): *draft cart
screen + sync sheet (from approved design), initData auth, deep links.*

Everything below the surface already works and is verified end to end. This plan is
about giving the existing pipeline a second face, not about changing what it does.

---

## Task 0 — Handlers return domain objects — **DONE 2026-08-18**

Landed before any frontend existed, which was the whole point: cheap with one consumer,
expensive with two.

`bot/outcomes.py` holds `DraftReady | PreviewReady | Synced | Spoke`. `render.to_reply`
is the only place a decision becomes markup, and it owns `Reply`, `Button` and the
keyboard. `handlers.py` has zero Telegram vocabulary — asserted by grep, not by hope.

The test migration was mechanical: wording assertions call `to_reply(...)`, and a new
`TestTheSeamASecondSurfaceWillUse` reads outcomes directly, which is the Mini App's
path. 746 tests green.

What follows is the original reasoning, kept because it is why the shape is this shape.

**The problem.** `bot/handlers.py` calls itself "the same seam the Mini App will use
later", and `bot/render.py` says the Mini App can reuse its rules. Neither is true as
written: handlers return `Reply(text=…)` where the text is **Telegram HTML** and
`Button.data` is a Telegram callback string. A Mini App needs `ResolvedCart` as JSON.
Rendering happens *inside* the handler, so the seam hands back markup.

**The shape.** Handlers return a typed outcome; Telegram rendering becomes an adapter
over it.

```python
# bot/outcomes.py (new) — no aiogram, no HTML
@dataclass(frozen=True)
class DraftReady:
    basket_id: int
    title: str
    cart: ResolvedCart
    budget_cap: int | None

@dataclass(frozen=True)
class PreviewReady:
    basket_id: int
    preview: SyncPreview

@dataclass(frozen=True)
class Synced:
    basket_id: int
    report: SyncReport

@dataclass(frozen=True)
class Spoke:            # plain prose: errors, prompts, /budget, free-form answers
    text: str
    needs_link: bool = False   # render the «Підключити Сільпо» button

Outcome = DraftReady | PreviewReady | Synced | Spoke
```

`bot/render.py` gains `to_reply(outcome) -> Reply` and keeps every existing rendering
function unchanged — they already take domain objects. `bot/bot.py` calls `to_reply`.

**Size, measured 2026-08-18:** 35 `return Reply(...)` sites in `handlers.py`; 73
handler call sites and 43 assertions on `.text`/`.buttons`/`.toast` across the 661
lines of `tests/test_handlers.py`.

**Migrating the tests is mostly mechanical.** `reply = await on_text(...)` becomes
`reply = to_reply(await on_text(...))` and the body is untouched. Do that first so the
suite stays green, then convert the tests that are really about domain facts (which
lines are in the cart, what is being removed) to assert on the outcome instead of on
Ukrainian substrings. The second half is optional and can land incrementally.

**Done when:** `komora/bot/handlers.py` contains no HTML and no `render_*` call, and
`tests/test_handlers.py` still passes.

## Task 1 — initData authentication

Telegram is the only identity provider (spec §8). The Mini App sends `initData`; the
backend verifies the HMAC against the bot token and maps to the encrypted Silpo tokens.

**Two things that do not carry over from the bot, and must be rebuilt here:**

1. **Ownership.** `handlers.on_callback` checks `basket.user_id != telegram_id` because
   a Telegram callback's basket id comes from the client. An HTTP endpoint has exactly
   the same exposure and inherits none of that check. Every basket-scoped route
   re-applies it.
2. **Confirmation.** Nothing reaches Silpo without an explicit second action, and
   nothing is deleted without being named first. The sync sheet is not decoration — it
   is the only place a **removal** is disclosed, and the only place the *live* cart is
   read back before a write. A Mini App that writes on one tap breaks the guarantee the
   bot makes.

`initData` is replayable within its TTL — check `auth_date` and reject stale payloads.

## Task 2 — Draft cart screen + sync sheet

Approved designs live in the claude.ai/design project `a416d167` (spec §8). The design
*system* — fonts, both palettes, semantic colours, trust copy — is fully specified in
spec §8 and needs no further decisions.

Platform rules are decided in the spec: `WebApp.expand()`, `viewportStableHeight` rather
than `100vh`, `disableVerticalSwipes()`, native `MainButton`/`BackButton`, Komora's own
palette with only the light/dark signal read from Telegram.

`web/` does not exist yet; `.gitignore` already anticipates `web/dist`.

## Task 3 — Deep links

Spec §8: nudges open the Mini App directly on a target screen.

---

## Constraints this plan must not break

- **One process.** `AuthorizationBridge` holds pending OAuth flows in memory, so the
  callback must land in the process that started the flow. Pinned deliberately at the
  `uvicorn.Config` in `main.py`. Scaling out needs shared state first — and it fails
  *silently*, which is why it is written down in three places.
- **The model still never acts.** Adding a surface adds no tools. Six read-only tools
  of the thirty-nine Silpo publishes; none of the eight that mutate a cart.
- **`core/` imports no web framework.** The Mini App API belongs in `api/`, reusing
  `core` through the same protocols the bot uses.

## Not in this plan

Habits (Plan 3, M3); meal plan, budget-week, deals and event intents (M4). The only
intent that exists is "stated basket".

**Still open from Plan 1:** dietary restriction filtering was removed rather than
shipped wrong — see the backend README's known issues. Rebuilding it needs one captured
payload from `silpo_get_my_food_restrictions`, which has never returned a populated
response, plus a rule for negation («молоко без лактози»). It is not a Mini App
dependency, but a dietary filter is exactly the kind of thing a UI invites.
