# Komora — backend

Personal grocery agent for the Ukrainian supermarket chain Silpo, built on their
official [MCP server](https://ai-factory.silpo.ua/docs/mcp). Telegram bot + Mini App.

Komora learns a household's shopping patterns from real Silpo receipt history and turns
any shopping intent into a reviewed, in-stock, optimized cart. Checkout itself happens in
Silpo — Komora prepares the cart and hands off.

> **Komora is a third-party project.** It is not affiliated with, endorsed by, or operated
> by Silpo or Fozzy Group.

## Status

Plan 1 Tasks 1–13 complete — the bot runs the whole loop. Task 14 is the manual
end-to-end run against real Silpo, which the bot has not yet met.

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
