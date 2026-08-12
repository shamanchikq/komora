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
| [Design spec](docs/superpowers/specs/2026-08-10-komora-design.md) | Architecture and product decisions. §3.1 is live-verified fact. |
| [Verified external facts](docs/superpowers/specs/2026-08-10-verified-external-facts.md) | Gemini models/pricing, `mcp` 2.0 traps, OAuth. |
| [Local models](docs/local-models-ollama-gemma.md) | Anything touching Ollama/Gemma. |
| [Plan 1](docs/superpowers/plans/2026-08-10-plan1-foundation-core-pipeline.md) | Current implementation plan (M0–M1). |

## Commands

All from `backend/`.

```bash
uv run pytest              # 525 tests
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
│   ├── passes/    restrictions -> resolve -> savings -> budget
│   ├── pipeline.py  composes the passes; load_context reads branch + timeslot
│   └── sync.py    preview + append to the real Silpo cart
├── db/            SQLAlchemy 2 + repos
├── api/           FastAPI — only the OAuth callback
├── bot/           handlers.py (Reply objects) + bot.py (the only aiogram file)
└── main.py        uvicorn + polling under one asyncio.gather
```

`core/` takes its dependencies as protocols, so the whole pipeline is testable with no
network, no bot and no LLM. The bot keeps that property: handlers are plain functions
over `(services, telegram_id, …)` returning a `Reply`, and aiogram appears only in
`bot.py` — so the conversation is tested without constructing a Telegram object.

## The rules that matter

**The LLM decides; it never acts.** Every *read* tool is open to it. **No write tool is
reachable** — calling one raises `ForbiddenToolCall`, so a hallucinated tool name cannot
mutate a cart. The model only ever emits a `DraftBasket` of *descriptions*; resolving
SKUs, stock, substitutions and prices is deterministic Python.

**Never guess an API shape.** Every parameter name assumed from a tool name in this
project turned out wrong. Capture it (`verify_mcp.py`), read the fixture, then write
code. Tool *descriptions* in `tools.json` are unusually detailed — read them.

**Never claim more than the data supports.** Cadence claims are about receipt history,
never the user's fridge. Savings come from `oldPrice − price`, never inferred coupon
matching. Partial sync is never reported as success.

**Nothing appears in a cart without a visible reason.** `reason_text` is required on
every line, in Ukrainian.

**Nothing reaches Silpo without confirmation**, and `clear_shopping_cart` is never
called unless the user explicitly asks to start over.

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

Plan 1 Tasks 1–13 complete, and Task 14's automated half done: the whole loop —
message → draft → pipeline → preview → **append** — **has run against the live Silpo
server** (`scripts/smoke_e2e.py --push`, 16/16). The write landed in a real cart with
the three pre-existing items untouched, and the cart was restored exactly.

Five defects found and fixed: cart writes carried undeclared fields, `error_of` passed a
Silpo 500 through as success, validation codes reached users untranslated, a coupon note
inlined three lines of bullets, and a successful sync with no checkout link gave no
reason for it.

**What remains is the Telegram surface**, which needs a bot token from @BotFather —
see the manual checklist in [backend/README.md](backend/README.md#manual-checklist).

Only the "stated basket" intent exists — meal plan, budget-week, deals and event
handlers are Plan 4. The Mini App is Plan 2; habits are Plan 3.

Known gaps, all deliberate — the current list lives in
[backend/README.md](backend/README.md#known-issues).
