# Komora

**A shopping agent that can suggest anything and change nothing.**

Python 3.14 · 793 tests · `mypy --strict` clean · the whole suite runs with no network, no model and no bot

You send a message — «купи молоко, хліб і щось до чаю» — and Komora comes back with a real
cart: actual products, actual prices, in stock at your store, each line saying why it's
there. You look it over, and only if you tap confirm does anything reach your real
shopping cart. Checkout stays where it belongs, in the shop's own app.

It's built against a **real third-party MCP server** — the Ukrainian supermarket chain
Silpo — which is what makes it worth reading. The tool schemas belong to someone else, the
failure modes are real ones, and nothing here works by guessing.

---

## The idea

Give a language model tools and it will eventually call the wrong one. The usual answer is
a carefully worded prompt. Komora's answer is to make the dangerous half unreachable:

| | |
|---|---|
| Tools the server publishes | **39** |
| Tools that can change a cart | **8** |
| Cart-changing tools the model can reach | **0** |
| Tools the model can reach at all | **6**, all read-only |

The model can search, browse and read prices. It cannot add, remove or clear anything —
those functions aren't merely left out of its list, they're refused at the boundary, so a
made-up tool name fails instead of doing damage.

What the model actually produces is a shopping list in words: `"молоко 2,6%"`, not a product
ID. Turning that into a specific product — searching, checking stock, handling
substitutions, picking a sane quantity — is ordinary Python you can test without a model
involved.

The practical effect: when the model gets something wrong, you get a worse *suggestion*, not
a wrong purchase. And the confirmation screen is built from a fresh read of your real cart,
not from what the write call claimed happened.

## How it's put together

```
komora/
├── core/          the domain — imports no web framework at all
│   ├── mcp/       talking to Silpo, and per-user OAuth
│   ├── llm/       one interface, two providers (hosted and local)
│   ├── agent/     the loop: read-only tools, and the guardrails
│   ├── passes/    resolve → verify → savings → budget
│   └── sync.py    preview, then write to the real cart
├── db/            SQLAlchemy 2
├── api/           FastAPI — OAuth callback + the Mini App API (initData → JSON)
├── bot/           the conversation, with Telegram in exactly one file
├── web/           the Mini App: Vite + React, served same-origin from web/dist
└── main.py        web server and bot polling in one process
```

Everything in `core/` takes its dependencies as interfaces, so the whole pipeline runs
against fakes. The bot keeps that property: each handler is a plain function that takes a
user and a message and returns a reply object, so the entire conversation is tested without
ever constructing a Telegram object.

The four passes run in one order and each is testable alone. Only *verify* asks a model, and
its answer is advice — it can flag a line and suggest a better search, but code decides what
happens next. A flagged line that can't be re-resolved is reported as "not found" rather
than quietly kept, because a wrong product presented as the right one is the worst possible
outcome: you might just buy it.

## The parts that were hard

**Two model providers, one interface.** A hosted model (Gemini) and a local one (Ollama)
sit behind the same small interface, and tool definitions travel as plain JSON Schema so
nothing upstream needs to know which is in use.

**Gemini doesn't accept plain JSON Schema.** It takes a narrower dialect, and the mismatch
isn't caught locally — the request simply fails on the server with an error that rarely
names the field at fault. So there's a converter that translates what it can, writes
unsupported rules into the description where the model can still read them, and refuses
loudly on anything it can't represent honestly.

**A subtle trap in Gemini 3.** Tool calls carry a signature that has to be handed back on
the following request. Read them via the obvious shortcut and the signature is lost, and the
*next* request fails — so single-turn calls look fine while every real conversation dies on
step two.

**Requests are the budget, not tokens.** The free tier counts requests per minute and per
day, so a longer prompt is free and a second round trip isn't. That shapes the design: one
check covers a whole basket instead of one per line, and extra hints ride along inside calls
that were happening anyway.

**Small things that prevent real bugs.** Money is `Decimal` and quantities are `float`, and
multiplying them directly raises a `TypeError` on purpose. Timestamps are UTC or they raise.
Stored access tokens are encrypted and tied to their owner, so a copied value fails to
decrypt rather than quietly granting someone else's access.

## Running it

Everything from `backend/`, using [uv](https://docs.astral.sh/uv/):

```bash
uv run pytest              # 793 tests, no network needed
uv run ruff check .
uv run mypy komora
```

The whole loop against the live server, minus Telegram — it stops at the confirmation
preview, exactly where the bot stops before your second tap. Add `--no-llm` to exercise the
shop half with no model at all:

```bash
uv run python scripts/smoke_e2e.py
```

Running the bot itself needs a Telegram token, a Gemini key (or a local Ollama model) and a
public HTTPS address for the OAuth callback — see [`backend/.env.example`](backend/.env.example)
and [`backend/README.md`](backend/README.md).

## Where it stands

The full loop works against live Silpo: link your account, send a sentence, review the
draft, confirm, and the products land in your real cart with a checkout link. Editing in
plain language works too, including removing something after it's already been sent.

The Mini App exists: initData authentication, the draft-cart screen and the sync sheet —
built in `web/` to an approved design and served same-origin from `web/dist`. It has not
yet been verified on a live device. Not built yet: Mini App deep links, the habits
engine, and the meal-plan, budget-week and deals flows. Open problems are listed honestly
in [`backend/README.md`](backend/README.md#known-issues), including the ones still unsolved.

## Documentation

| Document | What it covers |
|---|---|
| [Silpo MCP reference](docs/silpo-mcp-reference.md) | Field names, call order, and what the API doesn't give you |
| [Local models](docs/local-models-ollama-gemma.md) | Running on Ollama, and why it's development-only |
| [Dev environment gotchas](docs/dev-environment-gotchas.md) | SDKs that differ from their own documentation |

Code, comments and commits are in English. The interface is in Ukrainian.

## License

MIT — see [LICENSE](LICENSE).

---

**Not affiliated with Silpo.** Komora is an independent third-party client of their public
MCP server, built for personal use. "Сільпо" and related marks belong to their owners. It
places no orders: checkout always happens in Silpo's own app or website.
