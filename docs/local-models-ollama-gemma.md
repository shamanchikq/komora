# Running Komora on a local model (Ollama + Gemma 4)

**Written 2026-08-10.** Operational reference for if/when Komora needs to run against a
local model instead of hosted Gemini. Re-verify before acting on it after ~Nov 2026.

Evidence here comes from three sources, kept distinct because they differ in strength:

- **[LIVE]** — measured against the running Ollama daemon on this machine
  (`backend/scripts/probe_ollama.py`, Ollama 0.32.7).
- **[DOCS]** — official Google / Ollama / llama.cpp documentation or source.
- **[BENCH]** — public benchmarks (BFCL), or a single practitioner run where noted.

---

## TL;DR

**Viable for development. Not viable as a production default.**

Local Gemma 4 handles Komora's *single-turn* shape well — it picks the right tool from 21
and fills a nested schema in Ukrainian in ~3 s. But Komora's real workload is **multi-turn**,
where the best local candidate scores **~45% episode completion** against 61–68% for hosted
frontier models. That means roughly **one basket flow in two is wrong**, before any Ukrainian
penalty. For a grocery order, that failure is silent and plausible — worse than an error.

**Do not go local to save money.** At ~$10/mo hosted, the engineering to close a 16–23 point
multi-turn gap costs years of that bill. The two defensible reasons are:

1. **Privacy / data residency** — users' receipt histories never leave the machine.
2. **Free, offline development** — no API key, no quota, fast iteration.

---

## 1. What Gemma 4 actually is [DOCS]

Released **2026-03-31**, **Apache 2.0** — commercially unencumbered, unlike the bespoke
Gemma Terms that governed Gemma 1–3.

| Size | Params | Context | Note |
|---|---|---|---|
| E2B | 2.3B effective / 5.1B | 128K | too small — see §5 |
| E4B | 4.5B effective / 8B | 128K | too slow — see §5 |
| **12B Unified** | 11.95B | 256K | the practical local default |
| **26B A4B (MoE)** | 25.2B total / **3.8B active** | 256K | best quality/speed trade — decodes like a 4B |
| 31B Dense | 30.7B | 256K | heaviest |

**Function calling is official and first-class in Gemma 4** — trained special tokens, a
dedicated spec page, tool declarations in the chat template. This is the headline change
from Gemma 3, which had *no* official tool-calling and needed prompt hacks.

The wire format is a **custom token syntax, not JSON**:
`<|tool_call|>call:fn{param:<|"|>value<|"|>}<tool_call|>`. Ollama normalises this for you.
**Never hand-roll the parser** — escaping and nesting is exactly where a basket-of-line-items
call breaks.

**Ukrainian is claimed but unbenchmarked.** Gemma 4 is pre-trained on 140+ languages with
"out-of-the-box support for 35+", but Google publishes only MMMLU (31B 88.4, 26B A4B 86.3,
12B 83.4, E4B 76.6, E2B 67.4) and *no* Ukrainian-specific number. Nobody else has published
one either. The E2B/E4B drop is the warning sign about small tiers.

---

## 2. The integration traps — read this before writing any code

These bite before model quality ever becomes the limiting factor.

### 2.1 Use raw `httpx` or the `openai` package — NOT the `ollama` package [DOCS + LIVE]

This is the single most important item here, and it's why our probe succeeded where the
research predicted failure.

The `ollama` Python package's `Tool.Function.Parameters.Property` model has only
`{type, items, description, enum}` — **no `properties`, no `required`, no `anyOf`**. With
Pydantic's default `extra='ignore'`, everything else is silently discarded. A nested
`propose_basket` (object → array → objects) is told to the model as *"an object with no
fields"*. No exception, no warning.

The Go server does **not** have this limitation — `ToolProperty` is recursive and has an
explicit regression test for 3-level nesting. **The mangling is a client-library bug.**

> **[LIVE] confirmation:** our probe posted the nested schema over raw `httpx` to `/api/chat`
> and got back a correctly populated 3-line basket. The nested path works — over raw HTTP.

**Rule:** post the tool JSON yourself with `httpx`, or use the `openai` package (whose
`tools` param is a plain TypedDict passed through untouched). The `ollama` package only buys
auto-conversion of Python callables into schemas, which is useless for MCP-proxied tools we
already have schemas for.

### 2.2 Set `num_ctx` explicitly on every call [DOCS]

Default context is VRAM-tiered: **only 4096 tokens on machines with <23 GiB VRAM** (32768
for 23–47 GiB, 262144 for ≥47 GiB).

Tool schemas are re-rendered into the prompt on **every** request and are never truncated —
only chat messages get dropped when the prompt overflows, and `truncate` defaults to true, so
**Ollama silently discards your oldest messages rather than erroring**. With ~20 Silpo schemas
plus Ukrainian history, a dev laptop overflows 4096 immediately.

Set `options.num_ctx` ≥ 32768 (Ollama's own docs suggest ≥64000 for agent workloads) on every
native call. The OpenAI-compat endpoint offers **no way to set it** — that requires a Modelfile
with `PARAMETER num_ctx`.

### 2.3 Tool schemas are NOT passed through raw [DOCS]

Ollama unmarshals `parameters` into a typed Go struct keeping only: top-level `type`, `$defs`,
`items`, `required`, `properties`; per-property `anyOf`, `type`, `items`, `description`,
`enum`, `properties`, `required`.

Everything else is **silently dropped**: `$ref`, `oneOf`, `allOf`, `const`, `default`,
`format`, `pattern`, `minimum`/`maximum`, `minLength`/`maxLength`, `minItems`/`maxItems`,
`additionalProperties`, `title`, `nullable`.

**Never feed `model_json_schema()` to Ollama tools.** Pydantic emits `$defs` + `$ref`; the
`$defs` blob survives but every `$ref` pointer is deleted, degenerating the parameter to `{}`.
Hand-write inlined, `$ref`-free schemas. (Komora's plan already requires this for
`propose_basket`, for this reason and Google's own nested-schema warning.)

Portable workaround if you need depth: express nesting through **`items`**, which is typed
`Any`/`any` and passes through verbatim on both the Go and Python paths.

### 2.4 There is no `tool_choice` on any Ollama surface [DOCS]

Not on `/api/chat`, not on `/v1/chat/completions`, not on `/v1/responses`. You **cannot force**
a final `propose_basket` call. Options: prompt + client-side retry/validation, or the two-phase
design below.

`parallel_tool_calls` and OpenAI's `strict` are likewise unrecognised — **silently ignored,
not rejected**. Porting OpenAI code that relies on either as a safety guard loses that guard
with no signal.

### 2.5 Don't send `format` and `tools` together [DOCS, likely]

The grammar constrains generation to the schema, so the model can't emit the tool-call tag.

**Recommended architecture if the nested basket proves unreliable as a tool call:**

- **Phase 1** — tools-only agent loop (retrieval, multi-step).
- **Phase 2** — a tools-free call with `format=<nested JSON Schema>`.

`format` is passed through **verbatim** to llama.cpp's grammar converter, so it honours a far
richer keyword set than `tools`: `properties`, `required`, `additionalProperties`, `items`,
`prefixItems`, `minItems`/`maxItems`, `enum`, `const`, `oneOf`, `anyOf`, `$ref` + `$defs`,
`pattern`, `minLength`/`maxLength`, and `format: date|time|date-time|uuid`. Nested objects and
arrays-of-objects are fully supported.

Two sharp edges: `minimum`/`maximum` bind **only for `type: "integer"`** (there's an explicit
TODO for `number` in llama.cpp) — so use `integer` for `quantity`, not `number`. And `not`,
`if/then/else`, `multipleOf`, `uniqueItems` are silently ignored.

### 2.6 Response shape differs from OpenAI [DOCS]

- `message.tool_calls[].function.arguments` is a **real JSON object**, not a JSON string.
  Don't `json.loads()` it blindly — handle both, since the compat endpoint differs.
- Tool results go back as `{role: "tool", tool_name: "<name>", content: "<string>"}` — keyed
  by **`tool_name`**, not OpenAI's `tool_call_id`. The Python package doesn't model
  `tool_call_id` at all, so match results to calls **positionally / by name**, and append them
  in the order the calls arrived.

### 2.7 Silent no-ops to know about [DOCS]

- **`format` is silently ignored on the MLX runner** (Apple Silicon `*-mlx` models) and on
  Ollama Cloud. A Mac dev and a Linux CI box will behave differently with zero error signal.
  **Always validate parsed output client-side; never trust the grammar as a guarantee.**
- **`think: false` + `format` silently disabled schema constraints** on gemma4
  (ollama#15260, fixed after 0.20.0). Require Ollama > 0.20.0.
- **The Ollama/llama.cpp Gemma 4 chat template can silently drop tool-result messages** —
  the model never sees tool output, re-calls the same tool forever, and trips the max-steps
  guard. This looks like model stupidity and is an integration bug. Assert tool results
  round-trip into the next prompt, and treat "same tool, same args, twice in a row" as loop
  detection.

---

## 3. What we actually measured [LIVE]

`backend/scripts/probe_ollama.py` — 21 tool declarations, nested `propose_basket`,
Ukrainian prompts, raw httpx to `/api/chat`, `num_ctx=16384`, `think: false`.

| Model | Right tool from 21 | Nested schema | Ukrainian reasons | Latency |
|---|---|---|---|---|
| `gemma4:12b` | ✅ `propose_basket` | ✅ 3 lines | 3/3 | 3.3 s |
| `gemma4:e4b` | ✅ `propose_basket` | ✅ 3 lines | 3/3 | 26 s |
| `gemma4-agent:latest` | ❌ searched ×3 | — | — | 5.4 s |
| `qwen3.6:27b` | ❌ searched ×3 | — | — | 42.9 s |

Sample output: `{"description": "Молоко", "quantity": 1, "reason_text": "Ви попросили купити молоко."}`

**What this proves:** the raw-httpx path preserves nested tool schemas, Gemma 4 emits valid
Ukrainian into the right fields, and 21 tools is not too many.

**What this does NOT prove — and it's most of what matters:**

- Both pass criteria are **saturated metrics**. Single-call tool selection doesn't distinguish
  a 4B model from a frontier one. "Nested schema valid" passes under *any* grammar-constrained
  backend regardless of quality — the real failure is a well-formed basket with the *wrong
  items*.
- It was **single-turn**. Komora is multi-step; tool-result round-tripping was never exercised.
- **Gemma is the most prompt-format-fragile family on BFCL** — reformatting alone moves
  accuracy 34–67 points, so a two-prompt result is close to non-evidence.

Rerun it with:

```bash
cd backend && PYTHONIOENCODING=utf-8 uv run python scripts/probe_ollama.py gemma4:12b
```

(`PYTHONIOENCODING=utf-8` is required — the Windows console is cp1252 and can't print Cyrillic.)

### 3.1 Re-run against the *real* Silpo schemas — and it degraded [LIVE]

The probe above used 21 hand-written toy schemas. Repeating it through the actual
`OllamaClient` with **real captured Silpo schemas** told a different story:

| Setup | Result |
|---|---|
| **A** — permissive prompt + 5 real search tools + `propose_basket` | **No tool call.** Asked the user for store and delivery details instead. |
| **B** — prompt that forbids searching, same 6 tools | Called `propose_basket`, but **`description` was `null` on every line** — despite being `required`. |
| **C** — same prompt, `propose_basket` only | Correct: «Молоко», «Хліб», «Печиво до чаю», each with a reason. |

Three things follow, and they generalise beyond Gemma:

1. **Real tool schemas change behaviour.** `silpo_find_products_batch` requires
   `branchId`, `deliveryType` and a timeslot, so the model quite reasonably stalls
   asking how to fill them. The system prompt must state explicitly that the model does
   **not** search — resolving descriptions to SKUs is the pipeline's job.
2. **Tool-schema load degrades nested-output adherence.** Six tools versus one was the
   only difference between B and C, and it silently dropped a required field. This is
   the failure mode that matters: not a refusal, but *well-formed-looking output with
   holes in it*.
3. **Client-side validation is not optional.** `DraftBasket.model_validate` rejects B's
   payload (`lines.0.description: string_type`), which is exactly why malformed output
   must be a retry rather than something that reaches a cart.

This is also the clearest evidence yet for the promotion gate in §6: A and B would both
have passed a "did it call the right tool" check, and B would have passed a "schema
validates" check too if the schema had allowed nulls.

### 3.2 Hiding the context parameters fixed both problems [LIVE]

`find_products_batch` requires `branchId`, `deliveryType`, `timeslotStart` and
`timeslotEnd` — none of which a model can know. The agent loop now **strips those from
the declarations and injects them at dispatch**, so the model sees
`find_products_batch(products: string[])`.

Re-running case B through the real agent loop with 7 tools:

```
BASKET: Продукти до чаю та сніданку
  молоко          x1  Основа для сніданку або кави.
  хліб            x1  Свіжий хліб до обіду.
  випічка до чаю  x1  Солодка випічка або печиво до чаю.
```

Correct — and note `description` is now populated, where the same model with six
*unstripped* tools returned `null` for every line. Removing four required parameters
per schema recovered nested-output adherence, not merely the stalling.

The lesson generalises: **schema surface area, not tool count, is what small models
choke on.** Anything the application already knows should never appear in a
declaration.

---

## 3.3 Free-form product questions: local fails, Gemini passes [LIVE]

Measured 2026-08-12 on «яке грузинське вино є до 500 ₴?» — shipped prompt, shipped tool
declarations, fake Silpo, so the only variable is whether the model calls the search
tool and reports what it found. Three runs each.

| Model | Result |
|---|---|
| `gemini/gemini-3.1-flash-lite` | **3/3 answered correctly**, both wines with prices |
| `gemini/gemini-3.6-flash` | 1 run hit the step limit, 2 hit free-tier 429s |
| `ollama/gemma4:12b` | **0/3** — never searched; «не знайшов грузинських вин» |

gemma4:12b does not fail loudly here. It produces a fluent, apologetic Ukrainian reply
that reads like a real answer, having never called the search tool at all.

**A correction to an earlier version of this section.** It carried a table suggesting
that stripping unreachable advice from the tool descriptions, and sharpening the
prompt, changed which models searched. Re-running the identical harness twenty minutes
later inverted almost every cell — qwen3.6:27b went from asking-about-the-store to
answering correctly on the *unmodified* configuration. Those were single trials of a
non-deterministic behaviour, and the differences were noise. The variants are not
distinguishable at n=1; what is reproducible is the model gap in the table above.

The lesson is cheap to state and was expensive to learn: **one run per cell is not a
measurement.** Anything claimed about a model's tool-calling behaviour needs repeats
before it goes in a document.

Two details still worth keeping from that work:

* No model tried `get_products(toPrice=…)`, the parameter that actually filters by
  price. All of them put the budget into a free-text query.
* A confident wrong answer is the real risk. qwen3.6:27b once replied «Зараз у Сільпо
  немає вина за ціною до 500 ₴» with no search performed — worse than an unhelpful
  reply, because nothing marks it as a guess.

The stated-basket path — the one the product is built around — held up on gemma4:12b in
Telegram against the live server in the same period. It is the open-ended path that
needs a frontier model, which is what §6's gate already says.

## 4. The numbers that decide it [BENCH]

| Model | BFCL multi-turn |
|---|---|
| Claude-Opus-4.5 / Gemini-3-Pro / Claude-Sonnet-4.5 | 61–68% |
| **Gemma 4 26B-A4B** | **~45%** ⚠ single unreplicated quantized run |
| xLAM-2-8B-fc-r | 70% (rank 2 of 109) — but no Ukrainian |
| Gemma 3 (all sizes) | 5.75–10.75% — categorically unusable |
| MamayLM (best Ukrainian open model) | sits on a Gemma 2/3 base → unusable multi-turn |

BFCL multi-turn is **episode-level**, so ~45% ≈ one complete basket flow in two is wrong —
*before* an unquantified Ukrainian penalty (~25pp measured on comparable models for
non-English tool selection) and before the constraint tax (≤80.4% value accuracy even when
JSON validates).

**The core tension:** the tool-calling specialists (xLAM-2) are English-centric; the Ukrainian
specialists (MamayLM) sit on weak tool-calling bases. **No open model currently occupies both
axes.** Plan for a hybrid, not a single local model.

> **Evidence caveat, stated by the researcher:** the official BFCL-Result snapshot predates
> Gemma 4 entirely, so the single most decision-relevant number — the Gemma 3 → 4 delta —
> is the least well-evidenced. This space is also heavily polluted by SEO content farms
> publishing confident, unsourced "2026 leaderboards"; treat any tool-calling figure without
> a traceable primary source as marketing.

---

## 5. Model selection

- **`gemma4:12b`** — the practical local default. ~16–26 s per user turn at 5–8 steps.
- **`gemma4:26b-a4b`** — better quality at ~4B decode speed (MoE). Best local candidate; not
  currently pulled on this machine.
- **`gemma4:e4b` — avoid.** 2–3.5 minutes per turn at 5–8 steps. Disqualifying for a chat bot
  at any accuracy. Ollama's own 11-tool integration test skips 2B/3B models as unreliable.
- **Prefer base instruct models over agent-tuned variants.** `gemma4-agent` and `qwen3.6:27b`
  both searched instead of proposing. That is not reasoning — it's the documented
  **"Always-Call" pathology**, where small models fail hardest at knowing when *not* to act
  (Gemma-3-27b fabricates or flails in ~95% of missing-capability cases). It also happens to
  be wrong for Komora specifically: searching is the resolve pass's job, not the LLM's.

Per-model tool-count degradation is real but well above our surface — collapse starts around
200 tools. Qwen3-Coder reportedly degrades past ~5–6 and Ministral past 2, so **benchmark your
chosen model at the full 21-tool count** before committing. On gpt-oss/Harmony-template models,
nested tool params are flattened to `Record<string, any>` regardless of what you send.

---

## 6. The promotion gate

Before any local model is used beyond development:

- [ ] **50–100 scored Ukrainian multi-step episodes**, measuring **episode completion** — not
      tool-pick accuracy, not schema validity. Both of those pass unconditionally.
- [ ] Assert tool results round-trip into the next prompt (§2.7).
- [ ] Validate every `propose_basket` payload with Pydantic client-side; treat malformed
      output as a retry, not a crash.
- [ ] Verify no Ukrainian leaks into enums, ids, codes or units — parameter-value language
      mismatch is the dominant multilingual tool-calling failure. Ukrainian belongs in
      `description` and `reason_text` only.
- [ ] Confirm behaviour at the real 21-tool count and real conversation length, with
      `num_ctx` set explicitly.

No such Ukrainian agentic benchmark exists publicly. We would have to build it.

**A local model is never promoted to the `full` tier** (meal planning, long multi-step loops).
At most it serves `lite`.

---

## 7. Config

Switching is one env var — see `backend/.env.example`:

```bash
KOMORA_LLM_AGENT=ollama/gemma4:12b
KOMORA_LLM_VERIFIER=ollama/gemma4:12b
KOMORA_OLLAMA_BASE_URL=http://localhost:11434
```

`KOMORA_GEMINI_API_KEY` is not required when both tiers are `ollama/*`.

## 8. Related

- Design spec §4.1 (kept outside this repo) — provider abstraction and
  the read/write split that makes any local path arguable.
- Verified external facts (kept outside this repo) — Gemini,
  the `mcp` 2.0 SDK, Silpo's OAuth server.
- `backend/scripts/probe_ollama.py` — the probe, rerunnable.
