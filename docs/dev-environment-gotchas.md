# Development environment gotchas

Things that cost real time on this project and will cost it again. Windows + Ukrainian
+ a fast-moving SDK stack is a combination that produces failures which look like bugs
in your code and are not.

---

## Windows console encoding

The console is **cp1252** and cannot print Cyrillic. Any script that prints a product
name dies with `UnicodeEncodeError` — often *partway through*, which is worse than
failing at the start: `verify_mcp.py` could have crashed between adding a probe item
to a real cart and removing it.

```bash
PYTHONIOENCODING=utf-8 uv run python scripts/whatever.py
```

Scripts that print Ukrainian should force it themselves rather than rely on the caller:

```python
for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError):
        stream.reconfigure(encoding="utf-8", errors="replace")
```

## PowerShell is not bash

The shell here is **Windows PowerShell 5.1**:

| bash | PowerShell |
|---|---|
| `export VAR=value` | `$env:VAR = "value"` |
| `cmd1 && cmd2` | not supported (5.1) — use separate lines |
| `$(...)` | `$(...)` works, but quoting differs |

**Prefer `backend/.env` over shell exports entirely.** It behaves identically in both
shells, and it fixed a real bug: an inline-generated `KOMORA_TOKEN_ENCRYPTION_KEY`
meant every run produced a *new* key, so stored OAuth tokens could never be decrypted
and each run silently demanded a fresh Silpo login.

## Ruff and Cyrillic

`RUF001`/`RUF002`/`RUF003` flag "ambiguous unicode" — every Cyrillic `а`, `о`, `с`.
For a Ukrainian-language product this fires on the entire UI copy layer. Disabled in
`pyproject.toml`; do not re-enable.

## Alembic and the app must agree on which database

`alembic/env.py` reads `KOMORA_DATABASE_URL` from the **environment**. The app reads it
from **`.env`**, via `load_dotenv()`. If env.py does not load `.env` too, the two open
different files and nothing says so:

```
uv run alembic upgrade head   ->  success            (komora.db)
uv run alembic check          ->  no drift detected  (komora.db)
the bot, first basket         ->  OperationalError   (verify.db)
```

Both commands were telling the truth about a database nobody was using. env.py now
loads `.env`, `komora/db/migrate.py: assert_current` refuses to start on a stale schema
and names the file in the message, and no script calls `Base.metadata.create_all` any
more — that is what left a database with tables alembic had never heard of, so
`upgrade head` had nothing to apply.

**SQLite cannot add a NOT NULL column to a table with rows** unless the DDL carries a
`server_default`. A model-side `default=""` produces none, so autogenerate emits
`nullable=False` with no default and the migration passes on an empty database and
fails on every real one. Add `server_default` by hand.

## Working directory drift

`uv run` resolves the project from the current directory. After a `cd` to the repo root
for a git command, a later `uv run pytest` fails with `program not found` or picks a
different Python. Prefer absolute `cd` at the start of each command.

---

## Frontend toolchain (web/ exists now)

**A standalone node tarball needs `node` on `PATH` before npm works.** There is no
system node here; downloading the official tarball into `/tmp` and extracting it gives a
working `node`, but calling its `npm` directly dies with `env: 'node': No such file or
directory` — npm is a script whose shebang resolves through `env`. Export
`PATH=<tarball-dir>/bin:$PATH` for every shell that runs npm.

## FastAPI matches routes in registration order

**A `Mount("/")` added before the routes shadows every one of them.** Mounting
`StaticFiles(directory=dist)` at `/` at the top of `create_app` — so the Mini App could
be served same-origin — returned 404 for `/healthz`, `/auth/silpo/callback` and `/api/*`
alike: Starlette walks the route list top down, and the mount matches everything.
Register mounts **after** all routes; then the API keeps precedence and the catch-all
serves only what nothing else claimed.

---

## Library surfaces that differ from their documentation

Verified by introspecting installed packages, not by reading docs.

### MCP models: snake_case attributes, camelCase aliases

The wire format is not the attribute name:

| Wire / docs | Python attribute |
|---|---|
| `inputSchema` | `input_schema` |
| `outputSchema` | `output_schema` |
| `structuredContent` | `structured_content` |

The second kind is the dangerous one: `getattr(result, "structuredContent", None)`
returns `None` forever and silently falls through to a different code path.

### `Part.from_function_response` has no `id`

`ai.google.dev` shows `id=tool_call.id`. The real signature is
`(*, name, response, parts)` — passing `id=` raises `TypeError`. Build the response
directly when you need to match parallel calls:

```python
types.Part(function_response=types.FunctionResponse(id=…, name=…, response=…))
```

### Gemini 3: a function call carries a thought signature you must replay

Gemini 3 attaches an opaque `thought_signature` to the **`Part`** holding a function
call, and rejects the *next* request if it does not come back:

```
400 INVALID_ARGUMENT: Function call is missing a thought signature
```

The trap is `response.function_calls`. It is the obvious accessor, it yields clean
`FunctionCall` objects — and it drops the signature, because that lives on the
enclosing part, not on the call. Walk `candidates[].content.parts[]` instead and carry
the signature through your own message type:

```python
part.function_call          # name, args, id
part.thought_signature      # bytes | None — must be echoed back
```

Why this survives testing: a **single-turn** call works perfectly. Only the request
*after* a tool call is rejected, so every "does the client talk to Gemini" check passes
while every real agent loop dies on step two. It reached a live run here.

### Gemini 3: do not set `temperature`

Google warns it risks looping or degraded output, and it is deprecated on 3.6. The
reflexive `temperature=0` for "deterministic tool calling" is **counter-indicated**.
Set `thinking_level` explicitly instead — it defaults to `high`, which adds latency and
bills thinking tokens at the output rate.

### `ExceptionGroup` cannot hold a `BaseException`

`ExceptionGroup("x", [asyncio.CancelledError()])` raises `TypeError: Cannot nest
BaseExceptions in an ExceptionGroup` — the runtime returns a `BaseExceptionGroup`
instead. This matters twice over:

```python
except (Exception, BaseExceptionGroup):   # catches a cancelled session too
```

Anything that flattens transport errors must therefore check for non-`Exception`
causes and re-raise untouched (`core/mcp/gateway.py: _translated`), or a cancelled
task reports a Silpo outage instead of shutting down.

### Telegram: 4096 characters, and HTML that must be escaped

A cart with a reason under every line passes 4096 at around twenty items, and the API
rejects the whole message. Splitting has to happen on line boundaries — a cut inside
`<b>` breaks the parse. Product names carry `&` and `«»` routinely, so every dynamic
value is escaped (`bot/render.py: esc`).

Inline buttons accept only `http(s)` and `tg://` URLs. Silpo's `checkoutMobileLink` is
a `silpo://` deep link, so it can only be shown as text.

### Ollama: use raw `httpx`, not the `ollama` package

That package's tool-parameter model keeps only `{type, items, description, enum}` and
silently discards nested `properties` — a nested basket schema reaches the model as
"an object with no fields", with no error. The Go server preserves nesting fine.

Also set `options.num_ctx` explicitly: the default is **4096** below 23 GiB VRAM, and
overflow **silently drops the oldest messages** instead of erroring.

See [local models](local-models-ollama-gemma.md) for the full list.

---

## Testing patterns worth keeping

**Hermetic settings.** Every `Settings(...)` in tests passes `_env_file=None`. Without
it a developer's real `.env` leaks in and default assertions become unreliable — passing
locally, failing in CI, or vice versa.

**Fakes model verified behaviour, not convenient behaviour.** `FakeSilpo` *sets*
quantity rather than incrementing it, because that is what the live server does. A fake
that incremented would make the idempotency tests theatre.

**Pin upstream bugs with a test.** `test_stock_provider_wrongly_reports_an_expired_token_as_valid`
asserts the *broken* behaviour of `mcp` 2.0. When upstream fixes #3250 that test fails,
which is the signal to delete our workaround — rather than carrying it forever.

**Never assert truthiness on an API result.** See the Silpo reference §8: a failure can
be a perfectly truthy string — including one that carries none of the markers you
thought to check for.

**A fixture from a live account is a snapshot of a moment.** The captured timeslot
response has 0 available slots at midnight and 25 in the morning. Assert *structure*
against such a fixture and write the time-dependent case inline, or re-capturing breaks
the suite. This is the same trap as the sanitizer regression: a regenerable file is not
a constant.

**Test the failure mode you actually saw, with the string you actually got.** The
regression test for Silpo's 500 carries the real message verbatim, because the whole
point was that its wording is what defeated the classifier.

**Guardrails check the whole surface.** The write-tool test compares the allowlist
against the *full* captured tool list, so a future edit that adds a mutating tool fails
in CI rather than in production.
