# Komora

**An LLM agent architecture where the model decides and never acts.**

Python 3.14 · 754 tests · `mypy --strict` clean · no network, model or bot needed to run the suite

Komora turns a sentence — «купи молоко, хліб і щось до чаю» — into a reviewed, in-stock,
priced shopping cart. It is built on a **real third-party MCP server** (the Ukrainian
supermarket chain Silpo), which is what makes it interesting as engineering rather than as
a demo: the tool schemas are somebody else's, the failure modes are real, and none of it
can be made to work by guessing.

The grocery domain is the proving ground. The parts worth reading are the agent loop, the
provider-agnostic LLM layer, and the deterministic pipeline underneath them.

---

## The central constraint

Most agent frameworks give a model tools and hope the prompt holds. Komora makes the
dangerous half **structurally unreachable**:

| | |
|---|---|
| Tools the MCP server publishes | **39** |
| Tools that mutate a cart | **8** |
| Mutating tools the model can reach | **0** |
| Tools the model can reach at all | **6**, all read-only |

A call to anything outside that set raises `ForbiddenToolCall` before it reaches the
network — so a hallucinated tool name cannot mutate anything. The model's only output that
influences the cart is a `DraftBasket` of **descriptions** (`"молоко 2,6%"`), never a SKU.
Turning descriptions into real products — search, stock, substitution, quantity, price — is
ordinary Python that can be tested without a model in the room.

The practical consequence: every LLM failure mode degrades into a *worse suggestion*, never
into a wrong purchase. Nothing reaches the real cart without an explicit human confirmation,
and the confirmation sheet is generated from a fresh read of that cart rather than from what
the write call claimed.

## Architecture

```
komora/
├── core/          pure domain — imports NO web framework
│   ├── mcp/       protocol + the real client + per-user OAuth gateway
│   ├── llm/       LLMClient protocol; gemini/ and ollama/ implementations
│   ├── agent/     the loop: read tools only, propose_basket, guardrails
│   ├── passes/    restrictions → resolve → verify → savings → budget
│   ├── pipeline.py  composes the passes
│   └── sync.py    preview + write to the real cart
├── db/            SQLAlchemy 2 + repositories
├── api/           FastAPI — only the OAuth callback
├── bot/           handlers.py (pure functions → Reply) + bot.py (the only aiogram file)
└── main.py        uvicorn + Telegram polling under one asyncio.gather
```

`core/` takes every dependency as a `Protocol`, so the whole pipeline runs against fakes.
The bot keeps that property: handlers are plain async functions over
`(services, telegram_id, …)` returning a `Reply` object, and aiogram appears in exactly one
file — so the entire conversation is tested without constructing a Telegram object.

## The LLM layer

One `LLMClient` protocol, two implementations (hosted Gemini, local Ollama), and tool
parameters that travel as **raw JSON Schema** so no caller needs to know which is in use.
Two parts of this were considerably harder than they look:

**JSON Schema → Gemini's OpenAPI subset** ([`schema_map.py`](backend/komora/core/llm/gemini/schema_map.py)).
Gemini has no `oneOf`, `allOf`, `const`, `$ref`, `exclusiveMinimum` or `propertyNames`, and
the SDK does not normalise hand-built declarations — an unsupported keyword is not rejected
locally, the request just 400s server-side with a message that rarely names the field. The
converter degrades what it can into `description` prose rather than dropping constraints
silently, and raises on what it cannot represent faithfully.

**Gemini 3 thought signatures.** Reading tool calls off the convenient
`response.function_calls` accessor drops the `thought_signature` attached to the enclosing
part. Gemini then rejects *the next* request with `400 INVALID_ARGUMENT`, so every
tool-using conversation dies on its second step while single-turn calls look perfectly
healthy. The client reads calls off the parts instead and echoes the signature back.

**Request economics as a design constraint.** Gemini's free tier limits requests per minute
and per day, not tokens — so a longer prompt is free and a second round trip is not. The
whole-basket verification pass is one call for N lines; category hints ride along inside an
existing call rather than costing their own; and the two jobs point at two different models
because quota is keyed on `(project, model)`.

## Determinism where it counts

The passes are separate pure functions composed in one order, each testable alone:

```
restrictions → resolve → verify → savings → budget
```

Only `verify` consults a model, and its answer is advisory — it can flag a line and suggest
a better query, but deterministic code decides what happens next. A flagged line that cannot
be re-resolved is reported as "not found", never quietly kept, because a wrong product
presented as the right one is the worst outcome available: the user may simply buy it.

Money is `Decimal` and quantities are `float`, and multiplying them directly is a
`TypeError` on purpose. Timestamps are timezone-aware UTC or they raise. OAuth tokens are
AES-256-GCM at rest with the AAD bound to the owning user, so a ciphertext copied into
another row fails to decrypt rather than silently granting access.

## Running it

Everything from `backend/`. Dependencies are managed by [uv](https://docs.astral.sh/uv/).

```bash
uv run pytest              # 754 tests, no network required
uv run ruff check .
uv run mypy komora
```

The full loop against the live server, headless and read-only — everything except Telegram.
It stops at the confirmation preview, which is exactly where the bot stops before the user's
second tap. Add `--no-llm` to exercise the Silpo half with no model at all:

```bash
uv run python scripts/smoke_e2e.py
```

To run the bot itself you need a Telegram bot token, a Gemini API key (or a local Ollama
model), and a public HTTPS URL for the OAuth callback — see
[`backend/.env.example`](backend/.env.example) and [`backend/README.md`](backend/README.md).

## Status

The end-to-end loop works against live Silpo: account linking over OAuth, a sentence in,
a reviewed draft back, an explicit confirmation, then real products in a real cart with a
checkout hand-off. Plain-language edits to a draft work, including removals after a sync.

Not built: the Mini App, the habits engine, and the meal-plan / budget-week / deals intents.
Known gaps are tracked honestly in [`backend/README.md`](backend/README.md#known-issues) —
including the ones that are still open.

## Documentation

| Document | What it covers |
|---|---|
| [Silpo MCP reference](docs/silpo-mcp-reference.md) | Field names, call order, domain rules, what the API does not provide |
| [Design spec](docs/superpowers/specs/2026-08-10-komora-design.md) | Architecture and product decisions |
| [Verified external facts](docs/superpowers/specs/2026-08-10-verified-external-facts.md) | Gemini models and pricing, `mcp` 2.0 traps, OAuth |
| [Local models](docs/local-models-ollama-gemma.md) | Running against Ollama, and why it is development-only |
| [Dev environment gotchas](docs/dev-environment-gotchas.md) | SDK surfaces that differ from their own docs |

Code, comments and commits are English. The user interface is Ukrainian.

---

**Not affiliated with Silpo.** Komora is an independent third-party client of their public
MCP server, built for personal use. "Сільпо" and related marks belong to their owners. It
places no orders: checkout always happens in Silpo's own app or website.
