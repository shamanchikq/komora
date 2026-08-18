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

The design spec, the numbered plans and the design prompts live in `docs/superpowers/`,
which this repo ignores — it is its own private repo,
[shamanchikq/komora-docs](https://github.com/shamanchikq/komora-docs), so the public one
stays code and reference docs. Read them from disk; commits there never touch this repo.
**Plan 2 (Mini App) is current**, and its Task 0 is a refactor to do before any frontend
exists.

## Commands

All from `backend/`.

```bash
uv run pytest              # 748 tests
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy komora         # strict
uv run alembic upgrade head
uv run python -m komora.main   # the bot + the OAuth callback, one process
```

Against the live server — the whole loop minus Telegram, read-only, no API key
(`--push` writes to a real cart and then restores it):

```bash
uv run python scripts/smoke_e2e.py
```

CI runs all of the above plus `alembic check`, which fails if `tables.py` changed
without a migration.

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
│   ├── llm/       LLMClient protocol; gemini/ and ollama/ implementations
│   ├── agent/     the loop: read tools only, propose_basket, guardrails
│   │              + recap.py (what the model is told it did last turn)
│   ├── passes/    resolve -> verify -> savings -> budget
│   │              + categories.py (Silpo's taxonomy, beats free-text search)
│   │              + removals.py («прибери ковбаски» -> a product Komora synced)
│   ├── alternatives.py  «інший варіант» — same rule as resolve (`narrow`)
│   ├── pipeline.py  composes the passes; load_context reads branch + timeslot
│   └── sync.py    preview + append to the real Silpo cart
├── db/            SQLAlchemy 2 + repos
├── api/           FastAPI — only the OAuth callback
├── bot/           handlers.py (Outcome objects) + render.py (to_reply)
│                  + bot.py (the only aiogram file)
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

**A callback id is not proof of ownership.** Telegram callback data comes from the
client, so every basket action checks `basket.user_id` against the sender.

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

Still unticked: the re-auth path. The free-form question path passes on Gemini.

Only the "stated basket" intent exists — meal plan, budget-week, deals and event
handlers are Plan 4. The Mini App is Plan 2; habits are Plan 3.

Known gaps, all deliberate — the current list lives in
[backend/README.md](backend/README.md#known-issues).
