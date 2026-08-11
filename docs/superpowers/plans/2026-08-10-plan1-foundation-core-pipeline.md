# Komora — Plan 1: Foundation & Core Pipeline (M0–M1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working Telegram bot: user chats in Ukrainian → agent builds a draft basket → deterministic passes resolve it to real Silpo SKUs with coupons and budget → user confirms → items append to their real Silpo cart, ending with `checkoutWebLink`.

**Architecture:** Headless `core/` (no framework imports) receiving MCP + LLM clients as protocols; FastAPI only for the OAuth callback; aiogram bot on long polling as a thin adapter. LLM proposes (`propose_basket` interception), deterministic pipeline disposes; write tools are unreachable from the LLM path.

**Tech Stack:** Python 3.12, uv, FastAPI, aiogram 3, official `mcp` SDK (streamable HTTP + OAuth 2.1/PKCE/DCR), `google-genai` (Gemini 3.1 Flash-Lite / 3.6 Flash), SQLAlchemy 2 + aiosqlite + Alembic, `cryptography` (AES-GCM), pytest + pytest-asyncio + respx, ruff + mypy, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-10-komora-design.md` (committed). This plan implements milestones **M0 + M1** only.

---

## Context

Spec is approved: Komora, a grocery agent for Silpo built on their official MCP (`https://mcp.silpo.ua/mcp`). Repo currently contains only the spec — greenfield.

**Why M0+M1 as one plan:** M0 alone yields no user-visible value; M0+M1 yields the smallest honest product: the full intent→draft→passes→confirm→append loop over the bot in text form. Plans 2–4 (Mini App; Habits; Breadth+deploy) each build on this and get written after this plan lands, because two spec assumptions can only be verified live in M0 and may reshape later code:

1. **A1 — append semantics:** `silpo_add_or_update_cart_products` appends/upserts and never replaces (inferred from docs, not documented).
2. **A2 — schema mapping:** MCP tool JSON Schemas convert cleanly to Gemini's OpenAPI-subset function declarations (union-type edge cases).

Deferred to later plans: Mini App + initData auth (M2), purchases/habits/notifications tables and engine (M3), meal-plan/budget-week/deals/event intents, price memory, digests (M4), Hetzner deploy (M5). Budget pass here checks basket total against cap only (week-spend tracking needs M3 history).

**Deliberate M1 simplifications:** single intent — the user *states* what they need (the base all other intents build on); confirm UI is a bot inline keyboard; cart rendered as text; conversation history = last 20 turns from DB; no scheduler.

**Prerequisites (owner, before execution):** Telegram bot token from @BotFather; Gemini API key (AI Studio); Silpo account with purchase history; a tunnel for the OAuth callback (`cloudflared tunnel --url http://localhost:8000` or ngrok) — bot itself uses long polling, no public URL.

---

## File structure

```
backend/
├── pyproject.toml            uv project, deps, ruff/mypy/pytest config
├── .env.example
├── komora/
│   ├── config.py             Settings (pydantic-settings, KOMORA_ prefix)
│   ├── main.py               entrypoint: uvicorn + bot polling in one loop
│   ├── core/
│   │   ├── crypto.py         TokenCipher (AES-GCM)
│   │   ├── models.py         DraftBasket/DraftLine/ResolvedCart/ResolvedLine/SyncReport
│   │   ├── mcp/
│   │   │   ├── protocol.py   SilpoClient Protocol (read subset + cart ops)
│   │   │   ├── client.py     real impl over mcp SDK session
│   │   │   ├── auth.py       per-user OAuth: DBTokenStorage, callback bridge
│   │   │   └── errors.py     McpError, RateLimited, NotAuthenticated
│   │   ├── llm/
│   │   │   ├── protocol.py   LLMClient Protocol, Message/ToolDecl/ToolCall/LLMResponse
│   │   │   ├── gemini.py     google-genai impl, tier→model map, stable prefix for caching
│   │   │   └── schema_map.py json_schema_to_gemini() (A2 lives here)
│   │   ├── agent/
│   │   │   ├── loop.py       run_agent(): tool loop, max 8 steps, read-only registry
│   │   │   ├── tools.py      READ_TOOLS allowlist + propose_basket declaration
│   │   │   └── prompts.py    system prompt (Ukrainian)
│   │   ├── passes/
│   │   │   ├── restrictions.py  apply_restrictions(basket, restrictions) -> DraftBasket
│   │   │   ├── resolve.py       resolve_basket(basket, mcp, branch) -> ResolvedCart
│   │   │   ├── promos.py        apply_promos(cart, coupons) -> ResolvedCart
│   │   │   └── budget.py        apply_budget(cart, cap) -> ResolvedCart
│   │   └── sync.py           preview_sync() / execute_sync(): revalidate, append, report
│   ├── db/
│   │   ├── base.py           engine, session factory
│   │   ├── tables.py         users, conversations, draft_baskets, draft_items
│   │   └── repo.py           UserRepo, ConversationRepo, BasketRepo
│   ├── api/
│   │   └── app.py            FastAPI: /auth/silpo/start/{tg_id}, /auth/silpo/callback, /healthz
│   └── bot/
│       ├── bot.py            aiogram Dispatcher wiring
│       ├── handlers.py       /start, /cart, /budget, text messages, confirm callbacks
│       └── render.py         cart → Ukrainian text, totals, reasons
├── alembic/                  migrations
├── scripts/
│   └── verify_mcp.py         DAY-1: live verification of A1/A2, dumps schema fixtures
└── tests/
    ├── conftest.py           FakeSilpo, FakeLLM, in-memory DB fixtures
    ├── fixtures/mcp/         captured tool schemas + sample responses (from verify_mcp)
    └── test_*.py             per module
.github/workflows/ci.yml      ruff + mypy + pytest
```

Key shared types (defined once in Task 5, used everywhere — signatures must match):

```python
# komora/core/models.py
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel

ReasonKind = Literal["stated", "habit", "deal", "meal", "sub"]

class DraftLine(BaseModel):
    description: str                 # "молоко 2,6% ~1 л"
    quantity: float = 1
    optional: bool = False
    reason_kind: ReasonKind = "stated"
    reason_text: str                 # shown to user verbatim

class DraftBasket(BaseModel):
    title: str
    intent: str                      # "stated" in M1
    lines: list[DraftLine]

class ResolvedLine(BaseModel):
    product_id: str
    company_id: str
    branch_id: str
    name: str
    qty: float
    unit: str
    unit_price: Decimal
    reason_kind: ReasonKind
    reason_text: str
    substituted_from: str | None = None   # original name if substituted
    optional: bool = False
    unavailable: bool = False             # kept visible, excluded from totals

class ResolvedCart(BaseModel):
    lines: list[ResolvedLine]
    total: Decimal = Decimal("0")
    estimated_savings: Decimal = Decimal("0")
    savings_notes: list[str] = []
    warnings: list[str] = []              # degraded-mode labels, per spec §10

class SyncReport(BaseModel):
    ok: bool
    added: list[str]                      # product names
    failed: list[tuple[str, str]]         # (name, error)
    checkout_web_link: str | None = None
```

---

### Task 1: Repo scaffold + CI

**Files:** Create `backend/pyproject.toml`, `backend/.env.example`, `backend/komora/__init__.py` (+ empty `__init__.py` per package above), `.gitignore`, `.github/workflows/ci.yml`, `backend/tests/test_smoke.py`

- [ ] **Step 1:** `cd backend && uv init --no-workspace` then set `pyproject.toml`:

```toml
[project]
name = "komora"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115", "uvicorn[standard]>=0.30", "aiogram>=3.13",
  "mcp>=1.2", "google-genai>=1.0",
  "sqlalchemy[asyncio]>=2.0", "aiosqlite>=0.20", "alembic>=1.13",
  "pydantic-settings>=2.4", "cryptography>=43", "httpx>=0.27",
]
[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "respx>=0.21", "ruff>=0.6", "mypy>=1.11"]

[tool.ruff]
line-length = 100
[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]
[tool.mypy]
strict = true
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2:** Write `tests/test_smoke.py`: `def test_imports(): import komora  # noqa`
- [ ] **Step 3:** Run `uv run pytest -q` → PASS. Run `uv run ruff check .` → clean.
- [ ] **Step 4:** `.github/workflows/ci.yml`: on push/PR → `astral-sh/setup-uv@v5`, `uv sync`, `uv run ruff check .`, `uv run mypy komora`, `uv run pytest -q`.
- [ ] **Step 5:** `.gitignore` (python, .env, *.db, __pycache__, .venv, node_modules). `.env.example` with all `KOMORA_*` vars from Task 2. Commit `chore: scaffold backend, CI`.

### Task 2: Settings

**Files:** Create `komora/config.py`, `tests/test_config.py`

- [ ] **Step 1:** Failing test:

```python
def test_settings_reads_env(monkeypatch):
    for k, v in {"KOMORA_TELEGRAM_BOT_TOKEN": "t", "KOMORA_GEMINI_API_KEY": "g",
                 "KOMORA_TOKEN_ENCRYPTION_KEY": "k" , "KOMORA_PUBLIC_BASE_URL": "https://x.example"}.items():
        monkeypatch.setenv(k, v)
    from komora.config import Settings
    s = Settings()
    assert s.silpo_mcp_url == "https://mcp.silpo.ua/mcp"
    assert s.database_url.startswith("sqlite+aiosqlite")
```

- [ ] **Step 2:** Run → FAIL (no module). Implement:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    telegram_bot_token: str
    gemini_api_key: str
    token_encryption_key: str          # urlsafe-b64, 32 bytes decoded
    public_base_url: str               # tunnel URL, no trailing slash
    silpo_mcp_url: str = "https://mcp.silpo.ua/mcp"
    database_url: str = "sqlite+aiosqlite:///./komora.db"

    # "provider/model" refs — switching model OR provider is config, never code.
    # gemini/gemini-2.5-flash-lite  $0.10/$0.40   cost floor, no shutdown date
    # gemini/gemini-3.1-flash-lite  $0.25/$1.50   default; EOL 2027-05-07
    # gemini/gemini-3.5-flash-lite  $0.30/$2.50   newest lite (not cheaper)
    # gemini/gemini-3.6-flash       $1.50/$7.50   default full
    # ollama/gemma4:12b             free          local dev; verified working
    llm_lite: str = "gemini/gemini-3.1-flash-lite"
    llm_full: str = "gemini/gemini-3.6-flash"
    ollama_base_url: str = "http://localhost:11434"
    gemini_api_key: str = ""      # required only when a tier uses gemini/*

    model_config = SettingsConfigDict(env_file=".env", env_prefix="KOMORA_")
```

  Note `gemini_api_key` is no longer unconditionally required — an all-Ollama config must
  boot without one. Add a validator: a tier ref starting with `gemini/` requires the key;
  raise a clear error naming which tier if it's missing.
- [ ] **Step 2b:** Failing tests for a `parse_model_ref(ref) -> tuple[str, str]` helper:
  `"gemini/gemini-3.1-flash-lite"` → `("gemini", "gemini-3.1-flash-lite")`;
  **`"ollama/gemma4:12b"` → `("ollama", "gemma4:12b")`** (split on the *first* `/` only —
  Ollama tags contain colons, and future refs may contain slashes);
  a ref with no `/` raises; an unknown provider raises listing the known ones.
  Implement as a pure function. PASS.

- [ ] **Step 3:** Run → PASS. Commit `feat: settings`.

### Task 3: Token encryption

**Files:** Create `komora/core/crypto.py`, `tests/test_crypto.py`

- [ ] **Step 1:** Failing tests: round-trip; tamper raises; distinct ciphertexts for same plaintext (random nonce).

```python
import base64, os, pytest
from komora.core.crypto import TokenCipher

KEY = base64.urlsafe_b64encode(os.urandom(32)).decode()

def test_roundtrip():
    c = TokenCipher(KEY)
    assert c.decrypt(c.encrypt('{"access":"a"}')) == '{"access":"a"}'

def test_tamper_raises():
    c = TokenCipher(KEY); blob = bytearray(c.encrypt("x")); blob[-1] ^= 1
    with pytest.raises(Exception):
        c.decrypt(bytes(blob))

def test_nonce_uniqueness():
    c = TokenCipher(KEY)
    assert c.encrypt("x") != c.encrypt("x")
```

- [ ] **Step 2:** Implement with `cryptography.hazmat.primitives.ciphers.aead.AESGCM`: 12-byte `os.urandom` nonce prepended to ciphertext. ~15 lines.
- [ ] **Step 3:** Run → PASS. Commit `feat: AES-GCM token cipher`.

### Task 4: Database layer

**Files:** Create `komora/db/base.py`, `komora/db/tables.py`, `komora/db/repo.py`, `alembic/` (init), `tests/test_repo.py`

- [ ] **Step 1:** `tables.py` — SQLAlchemy 2.0 `Mapped`/`mapped_column`, Postgres-compatible types only:
  - `users`: `telegram_id: int` (BigInteger PK), `silpo_tokens: bytes | None` (LargeBinary), `branch_id: str | None`, `budget_weekly: int | None`, `created_at: datetime`
  - `conversations`: id PK, `user_id` FK, `role: str`, `content: str` (Text), `created_at`
  - `draft_baskets`: id PK, `user_id` FK, `title`, `intent`, `status: str` (draft|confirmed|synced|discarded), `created_at`
  - `draft_items`: id PK, `basket_id` FK, `product_id`, `company_id`, `branch_id`, `name`, `qty: float`, `unit`, `unit_price: Decimal` (Numeric(10,2)), `reason_kind`, `reason_text`, `substituted_from: str | None`, `optional: bool`, `removed: bool`
- [ ] **Step 2:** Failing repo tests against in-memory sqlite (`sqlite+aiosqlite://`): `UserRepo.upsert/get_tokens/set_tokens`, `ConversationRepo.append/last_n(20)`, `BasketRepo.create_from_cart(ResolvedCart)/get_active/set_status`.
- [ ] **Step 3:** Implement repos (thin, session-per-call via factory). Run → PASS.
- [ ] **Step 4:** `uv run alembic init alembic`, configure async URL from Settings, autogenerate initial migration; verify `alembic upgrade head` creates tables in a scratch file DB.
- [ ] **Step 5:** Commit `feat: db tables, repos, initial migration`.

### Task 5: Domain models

**Files:** Create `komora/core/models.py` (exact code in "File structure" above), `tests/test_models.py`

- [ ] **Step 1:** Failing test: `DraftBasket.model_validate` on a dict as the LLM would emit it (nested lines, defaults applied); `ResolvedCart` total is Decimal; `SyncReport` serializes.
- [ ] **Step 2:** Paste models from header section verbatim. Run → PASS. Commit `feat: domain models`.

### Task 6: MCP OAuth (per-user) + client

**Files:** Create `komora/core/mcp/auth.py`, `komora/core/mcp/errors.py`, `komora/core/mcp/client.py`, `komora/core/mcp/protocol.py`, `komora/api/app.py`, `tests/test_mcp_auth.py`

This is the riskiest task. **Amended 2026-08-10 after live verification** — see
[verified external facts §1–2](../specs/2026-08-10-verified-external-facts.md). The SDK
surface below is confirmed against the installed `mcp==2.0.0`, and Silpo's OAuth server was
probed directly. Do **not** consult any 1.x example: the names below are the real ones.

Confirmed API (introspected, not remembered):

```python
from mcp.client.auth import OAuthClientProvider, TokenStorage, AuthorizationCodeResult
from mcp.client.streamable_http import streamable_http_client   # NOT streamablehttp_client
import httpx2                                                    # NOT httpx

streamable_http_client(url, *, http_client: httpx2.AsyncClient | None = None,
                       terminate_on_close: bool = True)          # yields 2-tuple (read, write)
OAuthClientProvider(server_url, client_metadata, storage,
                    redirect_handler=None, callback_handler=None, ...)
```

Silpo's server (probed): DCR at `/register`, `refresh_token` supported, PKCE S256, all
endpoints at the origin, no `scopes_supported` — so send no `scope`, and upstream bug #3240
(pathful authorization servers) does not apply to us.

- [ ] **Step 1:** Pin `mcp==2.0.0` **exactly** in `pyproject.toml` (OAuth in 2.0 has ~12 open bugs; floating the version is how we inherit a new one). Re-introspect to confirm the surface still matches the block above:

```bash
uv run python -c "
import inspect, mcp.client.streamable_http as sh
from mcp.client.auth import OAuthClientProvider, TokenStorage, AuthorizationCodeResult
print(hasattr(sh,'streamable_http_client'), hasattr(sh,'streamablehttp_client'))
print(inspect.signature(sh.streamable_http_client))
print(inspect.signature(OAuthClientProvider.__init__))
print(sorted(m for m in dir(TokenStorage) if not m.startswith('_')))
print(list(AuthorizationCodeResult.model_fields))"
```

Expected: `True False`, then the signatures above, `['get_client_info','get_tokens','set_client_info','set_tokens']`, `['code','state','iss']`. **If this disagrees, stop and re-verify before writing code.**
- [ ] **Step 2:** Failing tests for `DBTokenStorage` — three distinct behaviours, all load-bearing:
  1. tokens round-trip per `telegram_id` and are encrypted (assert raw DB bytes ≠ plaintext JSON);
  2. **`get_client_info`/`set_client_info` hit ONE shared row, not a per-user row** — write client info as user A, read it back as user B and assert it is the same registration. Without this, every Telegram user triggers a fresh Dynamic Client Registration against `mcp.silpo.ua`, which gets us rate-limited or banned;
  3. an absolute **`expires_at`** is persisted on `set_tokens` and exposed for reload — `OAuthToken` carries only relative `expires_in`, so expiry is unreconstructable after a restart.
- [ ] **Step 3:** Implement `DBTokenStorage(user_repo, cipher, telegram_id)` with `loaded_expires_at` retained after `get_tokens`. Run → PASS.
- [ ] **Step 3b (MANDATORY — works around open upstream bug #3250):** `OAuthClientProvider._initialize()` restores tokens but **not** `token_expiry_time`. Left as-is, `is_token_valid()` returns `True` for an expired token, the refresh branch is skipped, Silpo 401s, and the SDK runs the *full interactive flow* — meaning users get spammed with re-login links even though we hold a good refresh token. Fatal here because we build a provider per user per request.

```python
class PersistentOAuthClientProvider(OAuthClientProvider):
    """Restores absolute expiry that upstream _initialize() drops (bug #3250)."""

    async def _initialize(self) -> None:
        await super()._initialize()
        expires_at = getattr(self.context.storage, "loaded_expires_at", None)
        if self.context.current_tokens is not None and expires_at is not None:
            self.context.token_expiry_time = expires_at
```

  Failing test first: construct the provider with storage holding an *expired* token, call `_initialize()`, assert `context.token_expiry_time` is set and `is_token_valid()` is `False`. Assert the stock class fails this test — that comment must stay honest as the SDK evolves.
- [ ] **Step 4:** Callback bridge. The SDK generates OAuth `state` **internally and we cannot inject it**, but it hands us the full authorization URL in `redirect_handler` *before* awaiting `callback_handler` — so parse `state` out of that URL and use it as the correlation key. This is the linchpin of the multi-user design, since `callback_handler` takes zero arguments.
  - `redirect_handler(url)` → register `PENDING[state]`, send the URL to the user as a Telegram message. **Never** `webbrowser.open` (the SDK core never opens a browser; only its example CLI does).
  - `callback_handler()` → await that entry, return **`AuthorizationCodeResult(code, state, iss)`** — a pydantic model, *not* the 1.x tuple. Forward `iss` (RFC 9207) or validation may fail.
  - FastAPI `GET /auth/silpo/callback?code&state&iss` resolves the pending entry, returns a small "Готово — поверніться в Telegram" page. `GET /auth/silpo/start/{telegram_id}` kicks off the flow.
  - Client metadata **must** set `application_type="web"` — it defaults to `"native"` (loopback CLI), which a strict AS may reject for an HTTPS redirect URI.
  - Failing tests: callback resolves a waiting future; unknown `state` → 400; timeout path cleans up `PENDING`.
  - Document in a module docstring that `PENDING` is in-process and therefore single-worker only.
- [ ] **Step 4b:** Guard against the concurrency trap: the SDK's interactive flow runs inside `context.lock` *inside the HTTP request lifecycle*, so a tool call that triggers re-auth blocks a coroutine for the full human login time. Account linking must be its own explicit flow; any MCP call made from the agent path raises `NotAuthenticated` immediately instead of starting an interactive login. Failing test: an unauthenticated MCP call from the agent path raises rather than invoking `redirect_handler`.
- [ ] **Step 5:** `errors.py`: `NotAuthenticated`, `RateLimited(retry_after)`, `McpUnavailable`, `OAuthRecoveryNeeded`. `client.py`: `SilpoMcp` — async context manager wiring the **2.0** shapes:

```python
async with httpx2.AsyncClient(auth=provider, follow_redirects=True) as http_client:
    async with streamable_http_client(MCP_URL, http_client=http_client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
```

  Three things the 1.x-shaped plan got wrong and this step must respect: auth rides on the **httpx2** client (the transport has no `auth=`/`headers=`/`timeout=` kwargs), the transport yields a **2-tuple** (1.x had 3), and `httpx2` is a *different library* from the `httpx` we depend on elsewhere — never hand an `httpx.AsyncClient` to it. Do not import `create_mcp_http_client`; it lives in the private `mcp.shared._httpx_utils`.

  `call(tool_name, args) -> dict` with: 429 → `RateLimited` + exponential backoff (3 attempts, jitter, honour Retry-After), 5xx → retry, connection failure → `McpUnavailable`, and on `OAuthRegistrationError`/`invalid_client` → wipe the shared `client_info` row and raise `OAuthRecoveryNeeded` (upstream #3256: the client can otherwise never recover from an expired DCR secret). Unit-test retry and the recovery path with a stubbed transport; no live calls.
- [ ] **Step 6:** `protocol.py` — the `SilpoClient` Protocol used by everything downstream (typed methods will be finalized against fixtures in Task 7):

```python
class SilpoClient(Protocol):
    async def find_products_batch(self, queries: list[str]) -> list[dict]: ...
    async def get_replacements(self, product_slug: str) -> list[dict]: ...
    async def get_my_food_restrictions(self) -> list[dict]: ...
    async def get_my_coupons(self) -> list[dict]: ...
    async def get_promotions(self) -> list[dict]: ...
    async def get_my_shopping_cart(self) -> str: ...          # cart id
    async def get_shopping_cart_by_id(self, cart_id: str) -> dict: ...
    async def add_or_update_cart_products(self, cart_id: str, items: list[dict]) -> dict: ...
    async def list_tools(self) -> list[dict]: ...             # name + json schema
```

  (`dict` payloads deliberately loose until Task 7 captures real shapes; downstream passes only touch fields we've verified.)
- [ ] **Step 7:** Commit `feat: per-user MCP OAuth + resilient client`.

### Task 7: Day-1 live verification (A1/A2) — MANUAL GATE

**Files:** Create `scripts/verify_mcp.py`, `tests/fixtures/mcp/` (generated)

- [ ] **Step 1:** Write `verify_mcp.py` (run manually with tunnel up, real Silpo login):
  1. Full OAuth round-trip through our FastAPI callback (proves Task 6 end-to-end).
  2. `list_tools` → dump every tool's JSON Schema to `tests/fixtures/mcp/tools.json`.
  3. Call `find_products_batch(["молоко", "хліб"])`, `get_my_shopping_cart`, `get_shopping_cart_by_id` → dump sanitized responses to fixtures.
  4. **A1 probe:** record current cart contents → add 1× cheap item → re-read (assert existing items untouched, item added) → add same item again (assert qty upserted to 2, not duplicated row) → remove it (`silpo_remove_cart_products`) → re-read (assert cart restored). Print PASS/FAIL per assertion.
- [x] **Step 2: DONE 2026-08-11 — 17/17 passed, A1 CONFIRMED.** Adding a second product left the first in place (3 → 5 lines), pre-existing lines untouched, cart restored exactly. Full findings in [spec §3.1](../specs/2026-08-10-komora-design.md). The plan proceeds.

  **Carry these into Tasks 10 and 12** — each contradicts an assumption the plan was written on:
  - Re-adding a product **sets** quantity rather than incrementing it. Sync is therefore idempotent by construction (good for the retry path), but overlapping products do **not** sum — the confirm sheet must not promise addition for a product already in the cart.
  - Search returns `id`; the cart expects `productId`. Normalise at the client boundary or `add_or_update_cart_products` fails validation.
  - `resolve_basket` cannot search with a bare query: `find_products_batch` requires `branchId`, `deliveryType`, `timeslotStart`, `timeslotEnd`, sourced from the cart. Task 10 Step 3 must take a cart-derived context object, not just `branch_id`.
  - `ResolvedCart.total` must come from `cart.calculation.totalAfterDiscounts`, not a sum of line prices — that is the amount the user actually pays.
  - Cap quantities at `product.stock`; exclude plastic bags; surface `cart.calculation.validations[]`.
- [ ] **Step 3:** Sanitize fixtures (strip names/phones/addresses), commit `test: captured MCP schemas + A1 verification results` with a note in the commit body stating the observed A1 behavior.

### Task 8: JSON Schema → Gemini declarations (A2)

**Files:** Create `komora/core/llm/gemini/schema_map.py`, `tests/test_schema_map.py`

**Scope note:** this rewrite is **Gemini-specific** and lives inside the Gemini provider
package — Ollama takes raw JSON Schema and must not be routed through it. The exact
rewrite rules were measured against the SDK's own converter; see
[verified external facts §4](../specs/2026-08-10-verified-external-facts.md).

- [ ] **Step 1:** Failing tests using **real captured schemas** from `tests/fixtures/mcp/tools.json`: every read-tool schema converts without raising; properties/required/enum/items/description preserved; `anyOf: [{type: X}, {type: "null"}]` → nullable X; other unions → `type: "string"` with the union described in `description`; `$ref` inlined; unsupported keywords (`additionalProperties`, `$schema`, `const`) stripped (`const` → single-value `enum`).
- [ ] **Step 2:** Implement `json_schema_to_gemini(schema: dict) -> dict` as a pure recursive function (~60 lines). Run → PASS.
- [ ] **Step 3:** Commit `feat: schema mapper verified against live Silpo schemas (A2)`.

### Task 9: LLMClient protocol + Gemini and Ollama implementations

**Files:** Create `komora/core/llm/protocol.py`, `komora/core/llm/factory.py`, `komora/core/llm/gemini/client.py`, `komora/core/llm/ollama/client.py`, `tests/test_gemini_client.py`, `tests/test_ollama_client.py`, `tests/test_llm_factory.py`

**Two providers, one protocol.** Gemini is the production default; Ollama makes the dev
loop free, offline and API-key-less, and is the privacy story. Verified working locally
2026-08-10 (`gemma4:12b`: correct tool from 21, valid nested schema, Ukrainian, 3.3 s).

Ollama specifics: `POST {ollama_base_url}/api/chat` with `stream: false`, OpenAI-shaped
`tools: [{"type":"function","function":{name, description, parameters}}]` taking **raw
JSON Schema — no Gemini rewrite**, `options: {"num_ctx": …}`, and `think: false` (these
models are thinking-capable; leaving it on adds latency for nothing here). Tool calls
come back on `message.tool_calls[].function.{name,arguments}`, where `arguments` may be
a dict *or* a JSON string — handle both.

- [ ] **Step 0:** Failing test for `make_llm(ref, settings)`: returns a `GeminiClient` for `gemini/*`, an `OllamaClient` for `ollama/*`, raises on unknown provider. Both must satisfy `LLMClient` (assert via `isinstance` against the runtime-checkable protocol).

- [ ] **Step 1:** `protocol.py`:

```python
@dataclass
class ToolDecl: name: str; description: str; parameters: dict   # JSON Schema
@dataclass
class ToolCall: name: str; args: dict
@dataclass
class Message: role: Literal["user", "assistant", "tool"]; content: str; tool_call: ToolCall | None = None; tool_result: str | None = None
@dataclass
class LLMResponse: text: str | None; tool_calls: list[ToolCall]

class LLMClient(Protocol):
    async def complete(self, system: str, messages: list[Message],
                       tools: list[ToolDecl], tier: Literal["lite", "full"]) -> LLMResponse: ...
```

- [ ] **Step 2:** Failing tests with mocked `google-genai` transport: tool declarations pass through `json_schema_to_gemini`; tier maps to `Settings.gemini_model_lite/full`; **prompt assembly is byte-stable** (system prompt + tools serialized identically across calls — assert two builds are equal; this is what makes implicit context caching hit).
- [ ] **Step 3:** Implement `GeminiClient` (~80 lines): system+tools first (stable prefix), then conversation; map function-call parts → `ToolCall`; one retry on timeout/5xx then raise `LLMUnavailable`.
- [ ] **Step 4:** Run → PASS. Commit `feat: Gemini LLM client behind protocol`.

### Task 10: Passes — restrictions, resolve, promos, budget

**Files:** Create the four modules under `komora/core/passes/`, `tests/test_pass_restrictions.py`, `tests/test_pass_resolve.py`, `tests/test_pass_promos.py`, `tests/test_pass_budget.py`, extend `tests/conftest.py` with `FakeSilpo` (returns fixture data; call log for assertions)

- [ ] **Step 1 (budget — pure, do first):** Failing tests: no cap → unchanged; under cap → unchanged; over cap → `warnings` gains `"over_budget:{overage}"` and optional lines identified (cart unchanged otherwise — trimming is the user's tap, per spec §7.4). Implement `apply_budget(cart, cap: int | None) -> ResolvedCart`. PASS. Commit.
- [ ] **Step 2 (restrictions):** Failing tests: line matching a restriction term (case-insensitive substring on description, e.g. «арахіс») is dropped and a warning appended `"excluded:{desc}:{restriction}"`; no restrictions → identity. Implement `apply_restrictions(basket, restrictions: list[str]) -> DraftBasket`. (M1 matching is lexical; product-category matching arrives with richer data in later plans.) PASS. Commit.
- [ ] **Step 3 (resolve):** Failing tests against `FakeSilpo`: each `DraftLine.description` → one `find_products_batch` query batch (≤30 per call); best match in stock → `ResolvedLine`; out of stock → `get_replacements`, substitute chosen, `substituted_from` set, `reason_kind="sub"`, reason text «заміна — немає в наявності»; no replacement → line kept with `unavailable=True`, excluded from `total`; totals = Σ qty×unit_price of available lines (Decimal). Implement `resolve_basket(basket, mcp: SilpoClient, branch_id) -> ResolvedCart`. PASS. Commit.
- [ ] **Step 4 (promos):** Failing tests: coupon whose eligible product ids intersect cart → `estimated_savings` += computed amount, note appended («купон −25% на каву · −41,23 ₴»); percent + flat coupon kinds; coupon on substituted line → NOT applied, note «купон не діє на заміну»; MCP failure while fetching coupons → cart unchanged + warning `"degraded:coupons"` (spec §10 degraded mode). Implement `apply_promos(cart, coupons) -> ResolvedCart`. PASS. Commit `feat: deterministic pass pipeline`.

### Task 11: Agent loop with read-only tools

**Files:** Create `komora/core/agent/tools.py`, `komora/core/agent/prompts.py`, `komora/core/agent/loop.py`, `tests/test_agent_loop.py`, `FakeLLM` in conftest (returns scripted `LLMResponse` sequences)

- [ ] **Step 1:** `tools.py`: `READ_TOOLS: dict[str, str]` — allowlist mapping tool name → `SilpoClient` method for the M1 read set (`silpo_find_products_batch`, `silpo_get_products`, `silpo_get_product_details`, `silpo_get_promotions`, `silpo_get_my_coupons`, `silpo_get_categories`); declarations built from captured fixture schemas.

  Plus the local `propose_basket` declaration. **Hand-write its JSON Schema as a flat literal — do NOT derive it from `DraftBasket.model_json_schema()`.** Two independent reasons: (a) Pydantic emits `$defs`/`$ref` for the nested `DraftLine`, which Gemini's converter must then inline and which Gemma degrades on; (b) Google's own Gemma 4 cookbook explicitly warns that auto-generated schemas "may not always meet specific expectations regarding complex parameters" and recommends hand-defining nested properties. Add a test asserting the hand-written schema contains no `$ref`/`$defs` and that a `DraftBasket.model_validate` round-trip of a conforming payload succeeds — that keeps the literal honest against the model without inheriting its schema shape.

  Give each field an explicit **language contract** in its `description` (Ukrainian prose belongs in `description`/`reason_text`; never in enums, ids, codes or units). The dominant multilingual tool-calling failure is parameter-value language leakage, and it must be validated post-hoc rather than trusted.
- [ ] **Step 2:** Failing guardrail tests (the spec §11 must-have):
  - registry passed to the LLM contains **no** tool whose name matches `add|update|remove|clear|certificate` write patterns — assert against the *full* fixture tool list so a future allowlist edit that sneaks a write tool in fails CI;
  - scripted `FakeLLM` returns a `ToolCall(name="silpo_clear_shopping_cart")` → loop raises `ForbiddenToolCall`, nothing dispatched.
- [ ] **Step 3:** Failing behavior tests: `propose_basket` call → loop returns `AgentOutcome(basket=DraftBasket, reply=None)` without dispatching to MCP; plain text answer → `AgentOutcome(basket=None, reply=text)`; read tool call → dispatched to `FakeSilpo`, result appended as tool message, loop continues; >8 steps → `AgentOutcome(reply=fallback_text)`.
- [ ] **Step 3b (tool-result round-trip assertion):** A dedicated test that the content of a tool result actually appears in the messages sent on the *next* LLM call. This looks redundant against a `FakeLLM` — it is not. The Ollama/llama.cpp Gemma 4 chat template has been reported to **silently drop tool-result messages**, so the model never sees tool output, re-calls the same tool forever, and trips the max-steps guard. That presents as model stupidity and is actually an integration bug. Assert `FakeLLM.calls[-1]` contains the tool payload, and treat "same tool called with identical args twice in a row" as a loop-detection error rather than letting it burn all 8 steps.
- [ ] **Step 4:** Implement `run_agent(llm, mcp, history, user_msg) -> AgentOutcome` (~70 lines). `prompts.py`: Ukrainian system prompt — Komora identity, «пропонуй кошик через propose_basket», reasons required per line, honesty rules from spec §1/G2. PASS. Commit `feat: agent loop, write tools unreachable`.

### Task 12: Sync service

**Files:** Create `komora/core/sync.py`, `tests/test_sync.py`

- [ ] **Step 1:** Failing tests for `preview_sync(cart, mcp) -> SyncPreview`:
  - re-resolves current prices via `find_products_batch` on the cart's product ids; total drift > 2% → `SyncPreview.drift = (old, new)`; item now out of stock → listed in `preview.now_unavailable`;
  - reads existing Silpo cart → `preview.existing_count`, `preview.existing_total` («у кошику вже N позицій — не чіпаємо»).
- [ ] **Step 2:** Failing tests for `execute_sync(cart, mcp) -> SyncReport`:
  - happy path: one `add_or_update_cart_products` call with all available lines, `SyncReport.ok=True`, `checkout_web_link` extracted from re-read cart;
  - partial failure (FakeSilpo rejects item 3): `ok=False`, `failed=[(name, err)]`, added lists the successes — never a success report on partial (spec §10);
  - `unavailable` lines are never sent;
  - re-running `execute_sync` after partial failure retries only failed lines (idempotent via upsert semantics verified in A1).
- [ ] **Step 3:** Implement (~60 lines). PASS. Commit `feat: sync — preview, append, honest partial reporting`.

### Task 13: Bot — DONE

**Files:** Create `komora/bot/render.py`, `komora/bot/handlers.py`, `komora/bot/bot.py`, `komora/main.py`, `tests/test_render.py`, `tests/test_handlers.py`

**Amendments made during execution.** Three gaps in this task as written, all found
against the captured schemas:

1. **No concrete `SilpoClient` existed.** Task 6 built the transport (`open_session`,
   retry) and the Protocol, but nothing implementing it — so `main.py` had nothing to
   wire. Added `core/mcp/silpo.py` (+ `payload.py` for unwrap/error classification,
   promoted out of `verify_mcp.py`) and `core/mcp/gateway.py` for per-user sessions.
2. **`sync.py` was sending undeclared fields.** `add_or_update_cart_products` declares
   `productId`/`companyId`/`branchId`/`quantity`; the payload also carried `name` and
   `price`, which the A1 probe never sent. Fixed before it could fail live.
3. **Two read tools could not be dispatched.** `get_product_details` and
   `get_categories` both require `branchId`, which their Protocol signatures had no way
   to carry. Both now take the `SearchContext`, and `CONTEXT_TOOLS` drives the loop's
   dispatch.

Also added beyond the plan: `core/pipeline.py` (composes the passes, so `bot/` stays an
adapter and the Mini App can reuse it), `/budget` (without it the budget pass was
unreachable from the product), an ownership check on every callback, and message
chunking for Telegram's 4096-character limit.

- [x] **Step 1 (render, pure):** Failing tests: `render_cart(ResolvedCart)` → Ukrainian text: numbered lines with qty × price, reason on each line («— купуєте…»), substitutions marked «⇄ заміна (було: X)», unavailable struck via «✕ немає — не в сумі», footer totals + «Заощаджено ≈ …» + budget line when cap set; `render_sync_preview`, `render_sync_report` (partial failure lists failed items). Ukrainian plural helper `pl(n, "позицію", "позиції", "позицій")` with tests (1/2/5/11/21). Implement. PASS. Commit.
- [x] **Step 2 (handlers):** aiogram handlers with all services injected (repos, agent, sync, mcp-factory) so tests drive them directly with fake Update objects:
  - `/start`: if no tokens → explain why access is needed (spec J2 copy) + button linking `{public_base_url}/auth/silpo/start/{tg_id}`; if tokens → «Готовий — скажіть, що потрібно».
  - text message: persist to conversations → `run_agent` → basket? run pipeline (restrictions → resolve → promos → budget) → persist `draft_baskets` → reply `render_cart` + InlineKeyboard [«Надіслати в Сільпо»] [«Скасувати»] : else reply text.
  - callback `sync:{basket_id}`: `preview_sync` → if drift/unavailable → render preview + [«Все одно надіслати»] [«Скасувати»] : else straight to confirm sheet text «У кошику Сільпо вже N — не чіпаємо. Додати K?» [«Додати»] → `execute_sync` → `render_sync_report` + checkout link button.
  - callback `cancel:{basket_id}`: status→discarded.
  - `NotAuthenticated` from any MCP call → re-auth message with login button (spec §10 token-refresh failure path).
- [x] **Step 3:** Failing handler tests: /start unauthenticated shows auth button; text→basket→confirm→synced happy path; partial-failure path shows failed items and keeps basket status ≠ synced. Implement. PASS.
- [x] **Step 4:** `bot.py` + `main.py`: single asyncio entrypoint — `uvicorn.Server(api).serve()` and `dp.start_polling(bot)` under `asyncio.gather`, graceful shutdown. Commit `feat: bot — full text-cart loop`.

### Task 14: End-to-end smoke + README — AUTOMATED HALF DONE

**Amendment.** Most of what this task verifies has nothing to do with Telegram: the
OAuth gateway, the client's argument names, the agent, the passes and the preview. All
of it now runs headlessly against the live server via `scripts/smoke_e2e.py` (14/14,
2026-08-11/12), which also captured the three response shapes that were still being
guessed at. Four defects found and fixed:

1. **Cart writes carried undeclared fields** (`name`, `price`) — on the call the whole
   product depends on.
2. **`error_of` passed a Silpo 500 through as success.** It arrives as
   `"Error in get-time-slots: API returned 500 ..."` — a truthy string with no
   `MCP error` prefix and no `success: false`.
3. **Validation codes were shown to users untranslated** — «product.offer.stock.max».
4. **A coupon note inlined three lines of bullets** from `limitText`, and the coupon's
   own description was a fragment; the value lives only in `get_coupon_details`.

One planned check was **withdrawn as unsound**: an up-front timeslot comparison built
without `get_time_slots`'s `start` parameter reported a valid cart as expired, because
Silpo answers with the current day's window. Re-implemented with `start` set to the
cart's own slot, and verified against the live account.

Steps 1 and 2 below remain open: they need a Telegram bot token.

### Task 14 (original): End-to-end smoke (manual) + README

**Files:** Create `README.md` (run instructions), `docs/superpowers/plans/` copy of this plan

- [ ] **Step 1:** `uv run alembic upgrade head && uv run python -m komora.main` with tunnel up. Manual checklist against the real bot:
  1. /start → auth link → Silpo OAuth → «Готово» page → bot confirms.
  2. «Купи молоко, хліб і щось до чаю» → draft cart with reasons renders.
  3. Confirm → preview shows existing Silpo cart count → «Додати» → success + checkout link.
  4. Open Silpo app: items present, pre-existing items untouched. ← the product's core promise
  5. «Яке грузинське вино є до 500 ₴?» → free-form read-tool answer (J5).
  6. Kill DB tokens row → any request → re-auth prompt appears.
- [ ] **Step 2:** Record results in README (known-issues section). Commit `docs: README + e2e results`.

---

## Verification (whole plan)

- `uv run ruff check . && uv run mypy komora && uv run pytest -q` green locally and in CI.
- `scripts/verify_mcp.py` A1 assertions PASS, recorded in fixtures commit (Task 7 gate).
- Guardrail test proves write tools unreachable from the LLM registry (Task 11).
- Manual e2e checklist (Task 14) — item 4 is the acceptance criterion for the whole plan.

## Explicitly out of scope (next plans)

Plan 2: Mini App (draft-cart screen from approved design, initData HMAC auth, deep links, OpenAPI→TS codegen). Plan 3: purchases import + habits engine + scheduler + notifications + onboarding payoff. Plan 4: remaining intents (meal plan, event, deals, budget-week), price memory, digest, Hetzner deploy. Hackathon registration decision: owner, before 31 Aug.
