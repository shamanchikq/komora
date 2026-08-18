# Verified external facts — 2026-08-10

Companion to the Komora design spec (kept outside this repo). Everything here was
verified on 2026-08-10 against official documentation, live HTTP probes, or the actually
installed package in `backend/.venv` — **not** from model memory.

**Re-verify before relying on any of it after ~Nov 2026.** `google-genai` ships weekly and
`mcp` 2.0 OAuth is visibly unsettled.

---

## 1. Silpo's OAuth server — probed live ✅

`GET https://mcp.silpo.ua/.well-known/oauth-protected-resource`

```json
{"resource":"https://mcp.silpo.ua",
 "authorization_servers":["https://mcp.silpo.ua"],
 "bearer_methods_supported":["header"]}
```

`GET https://mcp.silpo.ua/.well-known/oauth-authorization-server`

```json
{"issuer":"https://mcp.silpo.ua",
 "authorization_endpoint":"https://mcp.silpo.ua/authorize",
 "token_endpoint":"https://mcp.silpo.ua/token",
 "registration_endpoint":"https://mcp.silpo.ua/register",
 "response_types_supported":["code"],
 "response_modes_supported":["query"],
 "grant_types_supported":["authorization_code","refresh_token"],
 "token_endpoint_auth_methods_supported":["client_secret_basic","client_secret_post","none"],
 "code_challenge_methods_supported":["plain","S256"]}
```

What this settles:

- **RFC 9728 metadata is published** → the SDK's discovery path works; no endpoint guessing.
- **Dynamic Client Registration is real** (`/register`) → no manual client provisioning with Silpo.
- **`refresh_token` is supported** → silent refresh is possible (subject to §2's blocker).
- **All endpoints sit at the origin, not under a path** → upstream bug #3240 (refresh POSTing
  to the wrong endpoint on pathful authorization servers) **does not affect us**.
- **PKCE S256 available.** Use S256, never `plain`.
- **No `scopes_supported`** → do not send a `scope` parameter.

---

## 2. `mcp` 2.0.0 — breaking changes vs. the 1.x API the plan assumed

Verified by introspecting `backend/.venv` (mcp 2.0.0, released 2026-07-28).
**Every 1.x example, tutorial, and memorised snippet is wrong for us.**

| 1.x (what the plan assumed) | 2.0.0 (reality) |
|---|---|
| `streamablehttp_client` | **`streamable_http_client`** — old name does not exist |
| `httpx` | **`httpx2`** — `OAuthClientProvider` subclasses `httpx2.Auth` |
| transport yields 3-tuple `(read, write, get_session_id)` | **2-tuple `(read, write)`** |
| `streamablehttp_client(url, auth=…, headers=…, timeout=…)` | **`streamable_http_client(url, *, http_client, terminate_on_close)`** — auth rides on the httpx2 client |
| `callback_handler() -> tuple[str, str \| None]` | **`-> AuthorizationCodeResult(code, state, iss)`** |

Verified signature:

```python
streamable_http_client(url: str, *, http_client: httpx2.AsyncClient | None = None,
                       terminate_on_close: bool = True)

OAuthClientProvider.__init__(server_url, client_metadata, storage,
                             redirect_handler=None, callback_handler=None,
                             client_metadata_url=None, validate_resource_url=None)
```

`TokenStorage` is a Protocol with exactly four async methods: `get_tokens`, `set_tokens`,
`get_client_info`, `set_client_info`. `OAuthToken` fields: `access_token`, `token_type`,
`expires_in`, `scope`, `refresh_token`.

### Good news

The library **never opens a browser** — `webbrowser.open()` lives only in the example CLI.
Both handlers default to `None` and raise if missing. Our headless Telegram + FastAPI flow
is supported by design; no monkey-patching.

### Three traps that will bite Task 6

**(a) BLOCKER — upstream bug #3250, open and present in 2.0.0.**
`_initialize()` restores tokens but **not** `token_expiry_time`. With it `None`,
`is_token_valid()` returns `True` for an already-expired token → stale token sent → 401 →
the SDK runs the *full interactive flow* instead of refreshing. Because we build a provider
per user per request, silent refresh would essentially never work and users would be spammed
with re-login links. A subclass override restoring `token_expiry_time` from storage is
**mandatory**, not optional.

**(b) `TokenStorage` conflates two lifetimes.** `get/set_tokens` is **per-user**;
`get/set_client_info` is the **app-wide DCR registration**. A naive per-user implementation
registers a brand-new OAuth client with Silpo for every Telegram user — hundreds of junk
registrations, likely rate-limiting or a ban. Client info must come from **one shared row**.

**(c) `OAuthToken` carries only relative `expires_in`.** There is no absolute timestamp to
reconstruct after a restart. Persist a separate `expires_at` column. (This is also what
feeds the fix for (a).)

Also: `application_type` defaults to **`"native"`** (loopback CLI). We are a web client with
an HTTPS callback — set `application_type="web"` explicitly or a strict AS may reject
registration.

**Concurrency:** the interactive flow runs inside `context.lock` *inside the HTTP request
lifecycle*, so a tool call that triggers re-auth blocks a coroutine for the full human login
time. Keep account linking **out of the agent tool-call path**; fail fast with a
"link your account" message instead.

**Pin `mcp==2.0.0` exactly.** Open OAuth bugs: #3240, #3246, #3248, #3250, #3251, #3256,
#3260, #3261, #3263, #3264, #2779, #2858. Build a recovery path that wipes the shared
`client_info` row on `OAuthRegistrationError` / `invalid_client` (#3256 reports the client
can otherwise never recover from an expired DCR secret).

Do **not** import `create_mcp_http_client` — it lives in the private
`mcp.shared._httpx_utils`. Build `httpx2.AsyncClient(auth=provider, follow_redirects=True)`.

> **Two HTTP stacks now ship:** `httpx` 0.28.1 (ours) and `httpx2` 2.10.0 (mcp's).
> Anything handed to the MCP transport must be `httpx2`.

---

## 3. Gemini models — a correction to the spec

**The spec was wrong.** It claimed `gemini-2.5-flash-lite` retires 16 Oct 2026, sourced from
a third-party blog. The official deprecations table says otherwise:

| Model | Shutdown | Replacement |
|---|---|---|
| `gemini-2.5-flash-lite` | **No shutdown date announced** | — |
| `gemini-3.1-flash-lite` | **2027-05-07** | `gemini-3.5-flash-lite` |
| `gemini-3.1-flash-lite-preview` | 2026-05-25 | `gemini-3.1-flash-lite` |

Source: <https://ai.google.dev/gemini-api/docs/deprecations>

Corrected pricing (per 1M tokens, standard tier):

| Model ID (exact API string) | Input | Output | Notes |
|---|---|---|---|
| `gemini-2.5-flash-lite` | $0.10 | $0.40 | cost floor; **no** shutdown date |
| `gemini-3.1-flash-lite` | $0.25 | $1.50 | EOL 2027-05-07 |
| `gemini-3.5-flash-lite` | $0.30 | $2.50 | newest lite; a speed/quality play, **not** a price play |
| `gemini-3.6-flash` | $1.50 | $7.50 | strong tier |

**Model IDs are bare — there are no `-001` suffixes on Gemini 3.x.** Writing
`gemini-3.1-flash-lite-001` will 404. This also means the IDs are floating pointers: Google
can swap weights without changing the string, so pin behaviour with evals, not the ID.

**Decision (superseded 2026-08-12 — see the quota section below).** The original call was
`gemini-3.1-flash-lite` + `gemini-3.6-flash`. Now that free-tier quota is known to be
per-model and both tiers are actually instantiated, the tiers are
`gemini-3.5-flash-lite` (proposal) + `gemini-3.1-flash-lite` (verification): two
allowances instead of one, and each model on the job it measures better at. `3.6-flash`
is overkill for both. Both **must** stay env-overridable settings, never literals at call
sites. `gemini-2.5-flash-lite` remains a legitimate cost-floor fallback (6× cheaper on
output) — one env var away.

### Gemini 3 behavioural gotchas

- **`thinking_level` defaults to `high`.** Left alone this means multi-second latency and
  thinking tokens billed at the *output* rate. Set it to `minimal` or `low` on the cheap tier.
- **Do not set `temperature`.** Google explicitly warns to leave it at 1.0 on Gemini 3 —
  lower values risk "looping or degraded performance". The usual `temperature=0` reflex for
  deterministic tool calling is counter-indicated here. `temperature`/`top_p`/`top_k` are
  additionally **deprecated** for `gemini-3.6-flash` and `gemini-3.5-flash-lite`.
- **Thought signatures** must be echoed back when returning function results in a stateless
  multi-turn loop. The official SDK handles this automatically — another reason not to
  hand-roll HTTP.
- Function calling (parallel and sequential) has **full parity** between `3.1-flash-lite` and
  `3.6-flash`. Tier routing is a pure cost/quality decision, not a capability gate.

### Free-tier quota is per (project, model) — verified 2026-08-12

**Two models get two independent daily allowances.** This is the fact the two-tier
routing now exists to exploit: a basket costs one request on each tier, so pointing the
tiers at different models doubles baskets per day.

Google's prose does not say it. <https://ai.google.dev/gemini-api/docs/rate-limits> only
states *"Rate limits are applied per project, not per API key"* and *"Limits vary
depending on the specific model being used"*, and it no longer publishes the per-model
free-tier table at all. The evidence is Google's own quota machinery: free-tier 429
bodies name `GenerateRequestsPerDayPerProjectPerModel-FreeTier`,
`GenerateRequestsPerMinutePerProjectPerModel-FreeTier` and
`GenerateContentInputTokensPerModelPerMinute-FreeTier`, each carrying
`quotaDimensions: {location: "global", model: "<model-id>"}`. Project *and* model are
dimensions, and that covers RPD, RPM and TPM alike.

Three caveats worth keeping:

- **The bucket is keyed on the *resolved* model, not the string sent.** A request for
  `gemini-2.5-flash-image` produced a violation naming `gemini-2.5-flash-preview-image`,
  so a GA id and its preview alias can share one bucket. Whether dated snapshots and
  `-latest` aliases collapse the same way is **unverified** — do not assume an alias
  buys a second allowance.
- **Reports of "pooled" quota are a different mechanism.** Multiple API keys in one
  project genuinely do share (project is the dimension), and the Gemini CLI's
  Google-account path has a real 1000/user/day pool *across* the model family. Neither
  applies to the API-key path.
- **The numbers move.** Google stopped publishing them; third-party pages quoting
  15 RPM / 1500 RPD contradict what the AI Studio dashboard showed on this account
  (15 RPM / 500 RPD). Treat the dashboard as authoritative and never hard-code a budget.

### 3.5-flash-lite vs 3.1-flash-lite, measured on this project — n=5×2

Google published **no** multilingual, function-calling, instruction-following or
structured-output benchmark for either model; the released gains are agentic, coding and
long-context, run *"with high thinking"* while `3.5-flash-lite` defaults to `minimal`.
So neither of Komora's two jobs is covered by the vendor evidence, and
`scripts/compare_models.py` measures them instead. Two independent runs agree:

| | pass rate | `category_rate` | usable `better_query` |
|---|---|---|---|
| `gemini-3.1-flash-lite` | 5/5 | 0.29 – 0.55 | **1.00** |
| `gemini-3.5-flash-lite` | 4/5, 5/5 | **1.00** | 0.60 |

They are **complementary, not ranked**. 3.5 names a Silpo category on essentially every
line — the lever that lets `resolve` browse the taxonomy rather than search free text —
while 3.1 always returns a re-search query when the verification pass flags a mismatch,
and an empty one makes `pipeline._verified` drop the line as «Не знайшлося» instead of
repairing it. Hence lite=3.5 (proposal), full=3.1 (verification).

Watch for one open regression: `gemini-3.5-flash-lite` has been reported emitting its
function call as pseudo-XML in the **text** part rather than a `functionCall` part
(acknowledged by Google 2026-08-03, unresolved). The repro is narrow — Vertex,
`VALIDATED` tool mode, streaming — and none of those match this client, but it is
probabilistic. `compare_models.py` would show it as `no valid propose_basket`.

---

## 4. `google-genai` 2.17.0 — two APIs, and our choice

`google-generativeai` (the old `import google.generativeai`) is **dead** — support ended
2025-11-30. The correct import is `from google import genai`.

There are now **two** APIs:

- **Interactions API** (`client.aio.interactions`) — GA June 2026, "recommended for new
  projects". Server-side conversation state, native remote MCP, background tasks.
- **generateContent** (`client.aio.models.generate_content`) — labelled legacy but fully
  supported, not deprecated. Explicit context caching available here **only**.

**Decision: use `generateContent`.** Three reasons:

1. **Explicit caching is unavailable on Interactions.** Our system prompt + tool declarations
   are the one large byte-stable block we have.
2. **Interactions stores conversation history server-side by default (`store=True`).** For
   Ukrainian users' grocery and receipt data that is a privacy and data-residency decision,
   not a technical default to drift into.
3. We drive the loop manually anyway (spec §4.1), so we gain little from server-side state.

**Native remote MCP does not work for us.** In that mode *Google's* servers call
`mcp.silpo.ua` with a static bearer header. That cannot compose with per-user OAuth 2.1 +
PKCE + DCR and rotating refresh tokens, and it removes our ability to authorise individual
tool calls. Confirms the plan: own the OAuth, call `tools/list` ourselves, convert schemas
ourselves, expose them as plain `function` tools.

**Disable Automatic Function Calling.** It hides the loop — no per-step logging, no Telegram
"typing…" updates, no confirmation before side-effectful calls.

### Two documentation bugs to avoid

- `types.Part.from_function_response(...)` has **no `id` parameter**, despite ai.google.dev
  showing `id=tool_call.id`. It raises `TypeError`. For parallel calls where ids matter, build
  `types.Part(function_response=types.FunctionResponse(id=…, name=…, response=…))`.
- The SDK README is stale: it uses `interaction.outputs[-1].text` (the real field is `.steps`
  / `.output_text`), has a `function_call_part.function_call.args` bug, and uses
  `gemini-3.5-flash` throughout while the docs recommend `gemini-3.6-flash`.

### JSON Schema → Gemini converter spec (Task 8)

The SDK **does not normalise hand-built function declarations** — `t_json_schema()` is
literally `return origin`. The converter is 100% our responsibility; a bad schema simply 400s
server-side. Reference semantics, measured live against the SDK's own `process_schema`:

| Input | Required handling |
|---|---|
| `anyOf: [T, {type: "null"}]` | collapse to `T` + `nullable: true` |
| `const: "a"` (string) | rewrite to `enum: ["a"]` |
| `const: 7` (non-string) | **rejected** — `ValueError("Literal values must be strings")` |
| `$ref` / `$defs` | must be **inlined**; self-recursive `$ref: "#"` has no representation |
| `additionalProperties: true` | **rejected** on the Developer API |
| `additionalProperties: false` | slips through the SDK's truthiness guard — handle explicitly |
| `oneOf` / `allOf` | pass client-side, **fail server-side** — strip or rewrite to `anyOf` |
| object with >1 property | SDK injects `property_ordering` |

Two mutually exclusive target types: `types.Schema` (OpenAPI 3.0 subset — has `nullable`,
no `oneOf`) and `types.JSONSchema` (2020-12 subset — has `oneOf`, no `nullable`). Neither
supports `allOf`, `not`, `prefixItems`, `patternProperties`, `if/then/else`, `contains`,
`multipleOf`, or `exclusiveMinimum/Maximum`. `parameters` and `parameters_json_schema` are
mutually exclusive; the latter gives exact control.

**Contract test:** round-trip every captured Silpo tool schema through the converter (Task 8
already depends on Task 7's fixtures — this is why that ordering matters).

### Context caching

Implicit caching is on by default for 2.5+ but needs a **byte-stable prefix** and a minimum
input size (2048 tokens on 2.5 Flash, 4096 on 3.5 Flash; **unpublished for 3.6-flash** —
assume 4096). So: system instruction + tool declarations first and byte-identical across
turns; per-user data last. Interpolating a timestamp or user id into the system prompt
destroys every cache hit.

Explicit caching gives a guaranteed 90% discount but is **generateContent-only** — another
point for that choice. Telemetry field names differ by API:
`usage_metadata.cached_content_token_count` (generateContent) vs
`usage.total_cached_tokens` (Interactions).

> Our system prompt + ~20 tool declarations may land under the 4096-token minimum, in which
> case implicit caching never fires. **Measure before optimising** — the spec's claim that
> caching is "mandatory, not an optimisation" is premature until measured.

---

## 5. Toolchain — resolved empirically on Python 3.14.2 (Windows)

Every dependency published cp314 wheels; no source builds, including the Rust/C ones
(`cryptography`, `pydantic-core`, `aiohttp`). Every version floor in the plan was stale:

| Package | Plan assumed | Actual |
|---|---|---|
| `mcp` | >=1.2 | **2.0.0** |
| `google-genai` | >=1.0 | **2.17.0** |
| `mypy` | >=1.11 | **2.3.0** |
| `pytest-asyncio` | >=0.24 | **1.4.0** |
| `pytest` | >=8 | 9.1.1 |
| `fastapi` | >=0.115 | 0.141.1 |
| `aiogram` | >=3.13 | 3.30.0 |
| `cryptography` | >=43 | 50.0.0 |
| `alembic` | >=1.13 | 1.19.1 |

`google-genai` ships roughly weekly (four releases in three weeks) and the Interactions
surface is still moving — **pin it exactly and upgrade deliberately.**
