# Komora — backend

Personal grocery agent for the Ukrainian supermarket chain Silpo, built on their
official [MCP server](https://ai-factory.silpo.ua/docs/mcp). Telegram bot + Mini App.

Komora learns a household's shopping patterns from real Silpo receipt history and turns
any shopping intent into a reviewed, in-stock, optimized cart. Checkout itself happens in
Silpo — Komora prepares the cart and hands off.

> **Komora is a third-party project.** It is not affiliated with, endorsed by, or operated
> by Silpo or Fozzy Group.

## Status

Plan 1 complete through Task 14's automated half. The full loop — message → draft →
pipeline → preview → **append** — has been run against the live Silpo server, including
the write: items landed in a real cart, the three items already there were untouched,
and the cart was restored exactly. What it found along the way is in
[Known issues](#known-issues). What remains is the Telegram surface itself, which needs
a bot token (see [Manual checklist](#manual-checklist)).

**Before touching Silpo calls, read [docs/silpo-mcp-reference.md](../docs/silpo-mcp-reference.md)** —
field names, call order and domain rules, all verified against the live server. Every
parameter name assumed from a tool name in this project turned out to be wrong.

- [Design spec](../docs/superpowers/specs/2026-08-10-komora-design.md) — architecture and product decisions
- [Plan 1](../docs/superpowers/plans/2026-08-10-plan1-foundation-core-pipeline.md) — the current implementation plan
- [Dev environment gotchas](../docs/dev-environment-gotchas.md) — Windows, Cyrillic, SDK surprises
- [Verified external facts](../docs/superpowers/specs/2026-08-10-verified-external-facts.md) — Gemini, `mcp` 2.0, OAuth
- [Local models](../docs/local-models-ollama-gemma.md) — Ollama/Gemma

## Requirements

- Python 3.14 (pinned in `.python-version`; [uv](https://docs.astral.sh/uv/) installs it)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/)
- A Silpo account (OAuth is per-user, at runtime)

## Setup

```bash
cd backend
uv sync
cp .env.example .env   # then fill it in
```

Generate a token encryption key:

```bash
uv run python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

## Development

```bash
uv run pytest          # tests
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy komora     # types
```

The Silpo OAuth callback needs a public HTTPS URL. The bot itself uses long polling, so
only the callback needs tunnelling in development:

```bash
cloudflared tunnel --url http://localhost:8000
```

Put the resulting URL in `KOMORA_PUBLIC_BASE_URL`.

## Running the bot

```bash
uv run alembic upgrade head
uv run python -m komora.main
```

One process serves both: uvicorn holds the OAuth callback, aiogram polls for messages.

In Telegram: `/start` links the Silpo account (the login URL arrives as a message),
then plain text builds a basket — «купи молоко, хліб і щось до чаю». `/budget 1500`
sets a weekly cap. Nothing reaches the Silpo cart until «Надіслати в Сільпо» and then
«Додати в кошик» — two explicit taps, with a preview of the existing cart in between.

## End-to-end against the live server

`scripts/smoke_e2e.py` runs everything except Telegram — the OAuth gateway, the typed
client, the agent, the four passes and the confirmation preview — against real Silpo.
Read-only by default: it stops at the preview, which is where the bot stops before the
user's second tap.

```bash
uv run python scripts/smoke_e2e.py
```

No Telegram token and no API key needed — the LLM defaults to a local Ollama model.
`--push` appends to your real cart and then removes exactly what it added; `--no-llm`
skips the model and uses a fixed draft; `--message` changes the request.

### Manual checklist

What the script cannot cover. Needs `KOMORA_TELEGRAM_BOT_TOKEN` from
[@BotFather](https://t.me/BotFather). Run against **@moya_komora_bot** 2026-08-12.

- [x] `/start` → auth link → Silpo OAuth → «Готово» page → bot confirms.
- [x] «Потрібне молоко і яйця» → draft renders with a reason on every line.
- [x] «Надіслати в Сільпо» → preview → «Додати в кошик».
- [x] **The items appear in the real Silpo cart.** ← the product's core promise.
      Pre-existing lines surviving was verified separately by `smoke_e2e.py --push`,
      which ran against a cart holding three items and restored it exactly.
- [x] A follow-up edit in plain language — «можна замість 3 упаковок яєць додати
      тільки 2?» — produced a corrected basket with «Ви змінили кількість з 3 на 2
      упаковки» as the reason. Not in the original plan; it works anyway.
- [ ] «Яке грузинське вино є до 500 ₴?» → a free-form answer, no basket.
- [ ] Clear the user's `silpo_tokens` row → any message → the re-auth prompt appears.

The login link opens on **the machine running the bot**. `KOMORA_PUBLIC_BASE_URL` is
`http://localhost:8000` by default, and Silpo accepts a loopback redirect (registered
as a `native` client per RFC 8252), so no tunnel is needed — but `localhost` on a phone
is the phone. Testing from another device is the one case that needs `cloudflared`.

### Known issues

Found by the live runs on 2026-08-11/12, and left open deliberately.

- **A local model produces weaker baskets than the pipeline can rescue.** `gemma4:12b`
  emitted a line described as «Печиво або цукерки (до чаю)» — a compound phrase that
  matches no product, so it resolved to nothing and the user got «Не знайшлося». It
  also titled baskets «Завтрак» (Russian) and «Базові продукти</div>». The stray markup
  is now stripped at the model boundary; the language leak is not fixable from here.
  Both are consistent with
  [docs/local-models-ollama-gemma.md](../docs/local-models-ollama-gemma.md): local is
  for development, and the promotion gate has not been met.
- **Quantity defaults are the model's guess.** Asked for "milk and eggs" it chose three
  packs of eggs. Correcting it in plain language works, but nothing anchors a first
  guess to what the household actually buys — that is what the habits engine (Plan 3)
  is for.
- **The agent never re-plans a line that fails to resolve.** A description that matches
  nothing is reported, not retried with a simpler term. Cheap to add, but it belongs
  with the other intents in Plan 4.
- **A fresh OAuth provider per message** costs two discovery requests. Caching it would
  serve stale tokens immediately after linking.
- **`preview_sync` re-reads the cart but not current prices.** Drift is computed from
  the resolved lines, so a price that moved between drafting and confirming is only
  caught if the cart total moved with it.
- **Silpo's coupon endpoints are intermittently unavailable.** One run in three saw
  `get_my_coupons` fail outright. Komora degrades as designed — the cart is built, the
  coupon is listed without its enriched value — but the enrichment is best-effort.
- **Telegram is untested against the real API.** Everything below it is not.

## Running against a local model

Komora binds each LLM tier to a `provider/model` ref, so switching to a local Ollama model
is one env var — free, offline, no API key:

```bash
KOMORA_LLM_LITE=ollama/gemma4:12b
KOMORA_LLM_FULL=ollama/gemma4:12b
```

**Development only.** See [docs/local-models-ollama-gemma.md](../docs/local-models-ollama-gemma.md)
for the integration traps (they bite before model quality does), the measured evidence, and
the gate a local model must clear before going anywhere near production.

## Layout

```
komora/
├── core/      pure domain — no web framework imports
│   ├── mcp/   Silpo MCP client: typed wrappers, retry, per-user OAuth
│   ├── llm/   provider-agnostic LLM client (Gemini implementation)
│   ├── agent/ the agent loop; read-only tool access
│   └── passes/ deterministic pipeline: restrictions, resolve, promos, budget
├── db/        SQLAlchemy models and repositories
├── api/       FastAPI — OAuth callback and health
└── bot/       aiogram adapter — conversation and push
```

`core/` imports no web framework and receives its dependencies as protocols, so the whole
pipeline is testable without a server, a bot, or the network.

## License

Not yet chosen.
