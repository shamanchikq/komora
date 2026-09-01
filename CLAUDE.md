# Komora (Комора)

A personal grocery agent for the Ukrainian supermarket chain **Silpo**, built on their
official MCP server. Telegram bot + Mini App. **Third-party — not affiliated with Silpo.**

Komora turns any shopping intent into a reviewed, in-stock, optimized cart. Checkout
itself happens in Silpo: there is no order-placement tool, so we prepare the cart and
hand off with a checkout link.

**UI language is Ukrainian.** Code, comments and commits are English.

## Read before changing anything

| Document | When |
|---|---|
| [Silpo MCP reference](docs/silpo-mcp-reference.md) | **Any** Silpo call. Field names, call order, domain rules, what the API does not provide. |
| [Dev environment gotchas](docs/dev-environment-gotchas.md) | Windows/PowerShell, Cyrillic, SDK surfaces that differ from their docs. |
| [Local models](docs/local-models-ollama-gemma.md) | Anything touching Ollama/Gemma. |

The design spec, the numbered plans and the design prompts live in `docs/superpowers/`
(`specs/`, `plans/`, `design/`) — **a nested private repo**,
[shamanchikq/komora-docs](https://github.com/shamanchikq/komora-docs), which this one
gitignores so the public repo stays code and reference docs. Read them from disk. Two
things follow from the nesting, and both bite silently:

- **Editing one means committing inside that directory.** `git add -A` from the project
  root cannot see those files — the parent ignores the whole path — so a plan updated
  and "committed" from the root is not committed anywhere.
- **Absent after a fresh clone.** It is a separate repo and does not come along with
  this one: `git clone https://github.com/shamanchikq/komora-docs.git docs/superpowers`.

**Plan 2 (Mini App) is code-complete.** Handlers return domain objects
(`bot/outcomes.py`), the Mini App API authenticates `initData` and serves those
outcomes as JSON (`core/initdata.py`, `api/minapp.py`), the draft-cart screen + sync
sheet are built in `web/` (Vite+React, served same-origin from `web/dist`; build with
`npm run build`), and deep links open the app on one basket —
`?startapp=basket_<id>` → `GET /api/baskets/{id}`, behind the same ownership gate.
**None of it has been verified on a device**; the checklist is in
[backend/README.md](backend/README.md).

## Commands

All from `backend/`.

```bash
uv run pytest              # 882 tests
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy komora         # strict
uv run alembic upgrade head
uv run python -m komora.main   # the bot + the OAuth callback + the Mini App API, one process
```

The Mini App frontend builds separately, from `web/`: `npm ci && npm run build`
(Node ≥ 22) produces `web/dist`, which the app serves at `/` when present. `npm test`
runs Vitest over the pure modules — the money, quantity and warning-code formatting
that both surfaces are supposed to agree on, plus what a failed call may claim and
what a launch payload is allowed to mean. The page fetches nothing from an external
origin: Telegram's SDK and both typefaces are served from Komora.

Against the live server — the whole loop minus Telegram, read-only, no API key
(`--push` writes to a real cart and then restores it):

```bash
uv run python scripts/smoke_e2e.py
```

CI runs all of the above plus `alembic check`, which fails if `tables.py` changed
without a migration — and, in a second job, `npm ci && npm run build && npm test` in
`web/`. The frontend was outside CI entirely until 2026-08-26, so a broken Mini App
merged green while the backend job stayed green beside it.

Re-capture Silpo fixtures (read-only; `--probe-cart` also verifies cart append):

```bash
uv run python scripts/verify_mcp.py
```

A fixture captured from a live account reflects **the moment it was taken** — the
timeslot capture has a different number of available slots in the morning than at
midnight. Assert structure against fixtures, never a count that the clock decides.

## Architecture

```
komora/
├── core/          pure domain — imports NO web framework
│   ├── mcp/       protocol + silpo.py (the real client) + gateway.py (per-user OAuth)
│   ├── llm/       LLMClient protocol; gemini/, openrouter/ and ollama/ clients
│   ├── agent/     the loop: read tools only, propose_basket, guardrails
│   │              + recap.py (what the model is told it did last turn)
│   ├── passes/    resolve -> verify -> savings -> budget
│   │              + categories.py (Silpo's taxonomy, beats free-text search)
│   │              + removals.py («прибери ковбаски» -> a product Komora synced)
│   ├── alternatives.py  «інший варіант» — same rule as resolve (`narrow`)
│   ├── pipeline.py  composes the passes; load_context reads branch + timeslot
│   └── sync.py    preview + append to the real Silpo cart
├── db/            SQLAlchemy 2 + repos
├── api/           FastAPI — OAuth callback + the Mini App API (initData -> JSON outcomes)
│                    + serves web/dist at / when built
├── bot/           handlers.py (Outcome objects) + render.py (to_reply)
│                  + bot.py (the only aiogram file)
├── web/           the Mini App frontend: Vite+React, talks to api/ with initData
└── main.py        uvicorn + polling under one asyncio.gather
```

`core/` takes its dependencies as protocols, so the whole pipeline is testable with no
network, no bot and no LLM. The bot keeps that property: handlers are plain functions
over `(services, telegram_id, …)` returning an **`Outcome`** — a decision carrying
domain objects, with no idea how it will be shown. `render.to_reply` turns one into
Telegram HTML and a keyboard; a second surface serialises the same object instead. So
the conversation is tested without constructing a Telegram object, and `handlers.py`
contains no markup at all.

## The rules that matter

**The LLM decides; it never acts.** Every *read* tool is open to it. **No write tool is
reachable** — calling one raises `ForbiddenToolCall`, so a hallucinated tool name cannot
mutate a cart. The model only ever emits a `DraftBasket` of *descriptions*; resolving
SKUs, stock, substitutions and prices is deterministic Python.

**Never guess an API shape.** Every parameter name assumed from a tool name in this
project turned out wrong. Capture it (`verify_mcp.py`), read the fixture, then write
code. Tool *descriptions* in `tools.json` are unusually detailed — read them.

**A category is an aisle, not an answer.** `get_products` has no `query` parameter, so a
category browse comes back in Silpo's order knowing nothing about what was asked. It
narrows the search; it never replaces it. Both places that pick a product —
`passes/resolve.py` and `alternatives.py` — go through `resolve.narrow`, because
choosing a product and choosing the next one are the same question, and when they
answered it differently «⇄» toured a hundred cheeses in shelf order.

**Never claim more than the data supports.** Cadence claims are about receipt history,
never the user's fridge. Savings come from `oldPrice − price`, never inferred coupon
matching. Partial sync is never reported as success.

**Nothing appears in a cart without a visible reason.** `reason_text` is required on
every line, in Ukrainian.

**Nothing reaches Silpo without confirmation**, and `clear_shopping_cart` is never
called unless the user explicitly asks to start over.

**Komora removes only what Komora added.** An edit after a sync («заміни ковбаски на
салямі») has to take the old product out, or the cart just accumulates. The model names
what to drop in words; `passes/removals.py` matches that against *lines Komora itself
synced* and nothing else, because a product the user chose in the Silpo app is
indistinguishable from one of ours and deleting it is unrecoverable. Every match is
named on the confirmation sheet before the tap that sends it.

**One model request per turn is the budget.** Gemini's free tier limits requests per
minute and per day, not tokens — so a longer prompt is free and a second round trip is
not. The verification pass covers a whole basket in one call; category hints ride along
in the existing `propose_basket` call rather than costing their own.

**A basket id is not proof of ownership.** It arrives from the client on both surfaces —
a Telegram callback and an HTTP path are equally guessable — so every basket action goes
through `handlers._own_draft`, which re-derives `basket.user_id` against the sender and
refuses anything that is not an open draft.

## Conventions

- Money is `Decimal`; quantities are `float`. Use `ResolvedLine.line_total` — multiplying
  them directly is a `TypeError`, deliberately.
- Timestamps are timezone-aware UTC (`db/base.py: UtcDateTime`); naive datetimes raise.
- Secrets encrypted at rest with AAD bound to the owner (`core/crypto.py`).
- `mcp` is pinned **exactly** (open OAuth bugs); `google-genai` bounded `<3` (weekly releases).
- Python **3.14**, managed by uv via `.python-version`.

## Status

**Plan 1 is done.** The loop runs end to end through Telegram against live Silpo:
`/start` → OAuth → «Потрібне молоко і яйця» → reviewed draft → preview → items in the
real Silpo cart. Verified on **@moya_komora_bot**, 2026-08-12. Plain-language edits to a
draft («замість 3 упаковок — тільки 2») work, which the plan never asked for.

Editing a basket **after** it reached Silpo took a second pass: the reply «Готово.
Додано 4 позиції» was true and useless, because the sausage the user asked to replace
was still in the cart next to its replacement. Three things were wrong at once — history
recorded a draft as its title alone, so the model could not see what it was editing; no
bot path could take a product out; and the verification pass judged each line without
knowing the basket was a pizza. Verified on the live bot 2026-08-18: «заміни ковбаски на
салямі» took the sausage out of the real Silpo cart, left the milk alone, and reported
both. The re-auth path passed the same day, so the
[manual checklist](backend/README.md#manual-checklist) is complete.

`scripts/smoke_e2e.py` runs everything except Telegram headlessly against the live
server — use it before any manual run. Seven defects came out of these runs: cart writes
carried undeclared fields, `error_of` read a Silpo 500 as success, validation codes
reached users untranslated, a coupon note inlined three lines of bullets, a sync with no
checkout link gave no reason, savings printed as `15.000 ₴`, and the model's stray
`</div>` reached a basket title.

**Plan 2 (Mini App) is code-complete** — initData auth (`core/initdata.py`), the JSON
API (`api/minapp.py`, every basket route behind `handlers._own_draft`), the draft-cart
screen + sync sheet in `web/`, built to the approved design and its three correctness
passes (in `docs/superpowers/design/`), and deep links onto a named basket. The Mini App
has **not yet been verified on a live device** — the unchecked checklist lives in
[backend/README.md](backend/README.md), and it needs BotFather setup that no test can
stand in for.

A review of the finished surface found four defects in the frontend, all in the part of
it that restates a backend rule in TypeScript and none of which anything failed on:
money truncated where `core/money.py` rounds half-up, ten kilos of a weighted good drawn
as one, every `degraded:*` warning recursing into a blank screen, and a failed push
reported as «нічого не сталося» when what landed was unknown. `web/` now has a Vitest
suite over exactly those modules.

A second review, 2026-08-26, found twelve more — nothing failing, everything green.
Four mattered: an ordinary ⇄ on a line with no alternatives replaced the whole draft
screen with a sentence (the chat has scrollback, a Mini App does not); `Spoke.toast` was
serialised and then dropped, so a foreign basket and a spent one read identically
despite the backend distinguishing them; `POST …/lines/-1/remove` deleted the *first*
line and answered 200, because `remove` was the one position route without a bounds
check and SQLite reads a negative OFFSET as zero; and the `over_budget` warning was
stored rather than derived, so it outlived «Прибрати необовʼязкові» — the control that
exists to end it. The rest: no «Скасувати» anywhere in the Mini App (`api.cancel` was
dead code), `on_set_qty` snapping to no grid despite a docstring saying it did, an
unbounded `/api/draft` body, a confirmation sheet able to ask for «Додати 0 позицій»,
`on_trim_optional` with no positive test, a `--reserve` variable nothing read, and a
failed draft edit blaming Silpo for a request that never reached it.

A third review, 2026-09-01, found five — again nothing failing. One was a pipeline
defect: `resolve_basket` narrowed to the category **before** running the fallback
search, and `narrow([], shelf)` returns the shelf, so `candidates` was non-empty and
the retry loop broke on entry. The fallback query was fetched and never read, and any
line whose own wording missed took the head of its aisle in Silpo's order — the state
categories were narrowed to get away from. `core/alternatives.py` had the order right
all along, which is the tell: the two paths that must answer the same question
identically did not. The rest were input bounds and duplication — a basket id past
what a row can hold reached the driver (`OverflowError` on SQLite, a bigint `DataError`
on Postgres) instead of `_own_draft`, so it was an unhandled 500 rather than a
refusal; `stock: None` was read as *unlimited* rather than *unknown*, so a qty of 1e12
persisted a total no `Numeric(10, 2)` column can hold; and the over-budget overage was
printed twice on both surfaces, once as the budget caption and once as the warning
code the caption restates.

Fixed with them, and new since: a **partial** push now records which lines actually
landed (`DraftItem.synced`, migration `9b41c07ae512`), because a partly-landed basket
stays a `draft` on purpose and every draft surface drew it under «у кошику Сільпо нічого
не зміниться» — false for exactly those lines, and invisible to `synced_lines`, so
«прибери молоко» could not name a product Komora had put there minutes earlier.
`GET /api/baskets/active` and `/basket` make an open draft reachable again: the menu
button carries no launch payload, so the app always opened on compose, and typing there
discards the draft — the way back to a basket was to destroy it. And «⇄» in the Mini App
now opens a **picker** of up to five candidates (`core/alternatives.list_alternatives`,
`GET …/lines/{p}/alternatives`) instead of stepping one product forward per round trip;
the chat keeps cycling, because a Telegram keyboard cannot carry product names.

Only the "stated basket" intent exists — meal plan, budget-week, deals and event
handlers are Plan 4; habits are Plan 3.

Known gaps, all deliberate — the current list lives in
[backend/README.md](backend/README.md#known-issues).
