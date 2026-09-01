# Komora — backend

Personal grocery agent for the Ukrainian supermarket chain Silpo, built on their
official [MCP server](https://ai-factory.silpo.ua/docs/mcp). Telegram bot + Mini App.

Komora learns a household's shopping patterns from real Silpo receipt history and turns
any shopping intent into a reviewed, in-stock, optimized cart. Checkout itself happens in
Silpo — Komora prepares the cart and hands off.

> **Komora is a third-party project.** It is not affiliated with, endorsed by, or operated
> by Silpo or Fozzy Group.

## Status

Plan 1 complete, manual checklist included. The full loop — message → draft →
pipeline → preview → **append** — has been run against the live Silpo server, including
the write: items landed in a real cart, the three items already there were untouched,
and the cart was restored exactly. What it found along the way is in
[Known issues](#known-issues).

Plan 2 (the Mini App) is built through its third task: initData authentication, the JSON
API over the same handlers, and the draft-cart screen + sync sheet in `web/` — served
same-origin from `web/dist`. The Mini App has **not yet been verified on a live device**;
that checklist lives at the bottom of [its section](#mini-app-api).

**Before touching Silpo calls, read [docs/silpo-mcp-reference.md](../docs/silpo-mcp-reference.md)** —
field names, call order and domain rules, all verified against the live server. Every
parameter name assumed from a tool name in this project turned out to be wrong.

- [Dev environment gotchas](../docs/dev-environment-gotchas.md) — Windows, Cyrillic, SDK surprises
- [Local models](../docs/local-models-ollama-gemma.md) — Ollama/Gemma

## Requirements

- Python 3.14 (pinned in `.python-version`; [uv](https://docs.astral.sh/uv/) installs it)
- Node ≥ 22 — only to build the Mini App frontend (`web/`); the bot runs without it
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

## Mini App API

Same process, same handlers, second face. Every route requires
`Authorization: tma <initData>` — verified against the bot token (`core/initdata.py`,
±24 h freshness on `auth_date`) — and every basket route goes through the ownership
and confirmation gates the chat uses. Outcomes serialise as JSON with a `kind`
discriminator (`draft` / `preview` / `synced` / `spoke` / `alternatives`); money crosses
as strings.

| Route | Handler | Meaning |
|---|---|---|
| `POST /api/draft` `{text}` | `on_text` | a free-text turn → draft or prose |
| `GET /api/baskets/active` | `on_open_active` | the draft this user has open, if any — no id crosses the wire |
| `GET /api/baskets/{id}` | `on_open_basket` | where a deep link lands: show, change nothing |
| `POST /api/baskets/{id}/preview` | `on_preview` | first tap: live cart read into a sheet |
| `POST /api/baskets/{id}/push` | `on_push` | second tap: write to the real Silpo cart |
| `POST /api/baskets/{id}/swap` `{position}` | `on_swap` | next alternative for a line (the chat's ⇄) |
| `GET /api/baskets/{id}/lines/{p}/alternatives` | `on_list_alternatives` | up to 5 other products for a line; reads only |
| `POST /api/baskets/{id}/lines/{p}/choose` `{product_id}` | `on_choose_alternative` | pick one; the id is re-checked against a fresh list |
| `POST /api/baskets/{id}/lines/{p}/qty` `{qty}` | `on_set_qty` | stepper; snapped to the product's step and capped at stock, server-side |
| `POST /api/baskets/{id}/lines/{p}/remove` | `on_remove_line` | ✕ — edits the draft, never the Silpo cart directly |
| `POST /api/baskets/{id}/trim` | `on_trim_optional` | drop every optional line in one action |
| `POST /api/baskets/{id}/cancel` | `on_cancel` | discard the draft |

### The alternatives picker

«⇄» in the chat steps forward one product at a time and wraps. That is all a Telegram
keyboard can carry — the labels are full Ukrainian product names — but it means a user
who taps past the one they wanted goes round the whole list to reach it again, and each
tap is a fresh search for a ranked list the server built and discarded.

The Mini App draws the list instead: `GET …/alternatives` returns up to
`MAX_ALTERNATIVES` (5) candidates from the same `narrow` ordering `resolve_basket` uses,
the current product excluded, and `POST …/choose` puts one on the line. **Empty is not a
screen** — a line Silpo has nothing else for keeps the basket on screen and says so in a
quiet banner, which is the 2026-08-26 lesson about ⇄ restated.

`product_id` arrives from the client, so it buys nothing: `on_choose_alternative`
rebuilds the candidate list and refuses anything not in it. What can be chosen is
exactly what was offered, at the price Silpo quotes now rather than the one the picker
happened to draw.

### Getting back to a draft

`GET /api/baskets/active` and `/basket` in the chat. A draft used to be reachable only
from the message that announced it: the menu button carries no launch payload, so the
Mini App always opened on compose, and typing there calls `create_from_cart`, which
discards the previous draft. The way back to a basket was to destroy it. No id crosses
the wire here — the draft is looked up *by* the sender rather than checked against them.

### Deep links

`https://t.me/<bot>/<app>?startapp=basket_<id>` opens the Mini App on one basket.
Telegram delivers the payload as `start_param` **inside the signed `initData`**, so the
backend can read it — but it proves what the *link* said, never who may act on that
basket, and `GET /api/baskets/{id}` goes through `handlers._own_draft` exactly as a tap
in the chat does. The payload is `<kind>_<value>` so a nudge that wants to open a deal
or a habit (Plan 3/4) adds a kind rather than a second parser.

Set `KOMORA_TELEGRAM_MINI_APP_URL` to the link BotFather reports and every draft grows
an «Відкрити в Коморі» button. Leave it empty and nothing renders — the bot is complete
without the Mini App, and a guessed short name opens a 404 inside Telegram, which reads
as Komora being broken.

### Building the frontend

The Mini App lives in `web/` (Vite + React + TS). Node ≥ 22:

```bash
cd web && npm ci && npm run build   # → web/dist
cd web && npm test                  # Vitest over the pure modules
```

The suite is deliberately narrow: `format.ts`, `copy.ts`, `qtyLabel`, `hasChanges` and
`describeError` are where the frontend restates a backend rule in TypeScript, so they
are where the two surfaces can disagree without anything failing. Each case there was a
real divergence — money truncated where `core/money.py` rounds half-up, ten kilos drawn
as one, every `degraded:*` code recursing into a blank screen, a failed *push* reported
as if nothing had been sent when the request may well have landed, a confirmation label
counting to zero, and a failed *draft edit* blaming Silpo for a request that never went
near it.

CI builds and tests `web/` in its own job. Until 2026-08-26 it did neither, and the
backend job going green said nothing at all about the half of Plan 2 that users see.

`create_app` serves `web/dist` at `/` when it exists — same origin as `/api`, so no
CORS. Without it the process is API-plus-callback only. For live UI work,
`npm run dev` in `web/` proxies `/api` to `localhost:8000`.

### Manual checklist for the Mini App

**Not yet run.** Everything below needs a physical device and a published Mini App;
nothing in it can be verified from a test suite, which is exactly why it is written
down rather than assumed.

**Environment first.** Three things will waste the session if they are not done before
BotFather, all found by audit on 2026-08-26 rather than by reasoning:

- [ ] **A fresh timeslot in the Silpo cart.** The account's cart held a slot from eight
      days earlier, and Silpo answers a search made against a passed slot with **zero
      results for everything** — measured: «молоко» returned 0 on the stale slot and 30
      on a current one, same branch, same account, same minute. The pipeline is right
      about it (`timeslot:expired` is raised and shown) but no basket can be built at
      all, so the first real step of the checklist fails for a reason that looks like
      Komora being broken. Open the Silpo app, pick a branch and a slot, then start.
- [ ] **An HTTPS `KOMORA_PUBLIC_BASE_URL`.** Telegram will not accept an `http://` or
      loopback Web App URL, so the device test needs `cloudflared` even though local
      OAuth does not — see the note under the Plan 1 checklist.
- [x] ~~Clear the stored DCR registration after changing that URL.~~ **Now automatic.**
      The registration is app-wide and holds the `redirect_uris` it was created with, so
      moving the base URL left it pointing at a callback the process no longer serves —
      and a new authorization would present a redirect_uri Silpo never registered.
      `DBTokenStorage.get_client_info` now drops a registration that does not list the
      current callback and lets the SDK register afresh. Already-linked accounts are
      unaffected either way: refresh uses the client id, which only changes when a new
      registration is actually made.

Then, in BotFather:

- [ ] `/newapp` on the bot → a **short name** and the Web App URL
      (`KOMORA_PUBLIC_BASE_URL`, which serves `web/dist` at `/`).
- [ ] `/setmenubutton` → the same URL, so the chat has a door.
- [ ] `KOMORA_TELEGRAM_MINI_APP_URL` set to the `t.me/<bot>/<app>` link BotFather
      reports, and the process restarted. **Empty today** — until it is set the bot
      renders no «Відкрити в Коморі» button and the deep-link section below cannot run.

**Worth doing first, because it needs no phone, no tunnel and no BotFather.** Run the
app, then mint a signed launch URL and open it in a desktop browser:

```bash
uv run python -m komora.main
uv run python scripts/dev_miniapp_url.py --user <your telegram_id>
```

Telegram delivers `initData` in the URL **fragment**, so a payload signed with the bot
token is indistinguishable from a real launch to everything downstream: the same HMAC
check, the same ownership gate, real baskets in the real database against live Silpo.
The script deliberately omits `tgWebAppPlatform`, which keeps `telegramHost()` false so
the in-page fallback bar renders instead of a native MainButton no browser can draw.
`--basket <id>` produces what a «Відкрити в Коморі» deep link would.

Verified end to end this way on 2026-08-26: compose → a three-line draft off live Silpo
with a reason on every line → ⇄ swapped a line and left the basket on screen. It does
not replace the device checklist — the native MainButton, BackButton, the Telegram
palette signal and `viewportStableHeight` are exactly what it cannot exercise — but
everything below those is reachable without leaving the desk.

The URL is a **credential**: it grants that user's access for 24 hours. Development only.

The app itself:

- [ ] Menu button opens the Mini App; initData accepted (no 401).
- [ ] Compose → loading skeleton → draft with reasons on every line.
- [ ] Stepper on a weighted good moves by its step and stops at stock.
- [ ] ✕ removes a row from the draft; push then sends one line fewer.
- [ ] ⇄ opens the picker: up to five other products, the current one marked «зараз».
      Tapping one returns to the draft with that product, the same quantity and the
      same reason, and a toast naming it.
- [ ] ⇄ on a line Silpo has no alternative for keeps the basket on screen and says so
      in a quiet banner — it must NOT become a screen of its own.
- [ ] The menu button (no deep link) opens on the draft you already have, not compose;
      `/basket` in the chat does the same.
- [ ] After a **partial** push, reopening the basket marks the lines that landed with
      «✓ вже в кошику Сільпо» and drops the «нічого не зміниться» promise. Hard to
      stage on a device — `swallow=` in `tests/fakes.py` is how it is exercised.
- [ ] «Скасувати» on the draft and on the sync sheet discards it; the Silpo cart is
      untouched.
- [ ] Preview sheet names removals in the inverted panel; confirm label says
      «Прибрати N позицій» when nothing is added.
- [ ] Push lands in the real Silpo cart; both checkout buttons work.
- [ ] Onest and IBM Plex Mono render (they are served from `/assets`, not Google) and
      the palette follows the client's light/dark setting.
- [ ] **Nothing hides under the native MainButton.** The sticky summary bar is
      `position: fixed` against the *viewport*, while the page sizes itself against
      `viewportStableHeight` — whether Telegram shrinks the webview for the button or
      draws over it decides whether the two agree, and no test can answer that. A
      `--reserve` variable was written for this and read by nothing; it was removed
      rather than left looking like a solution. If the summary is obscured, the fix is
      to offset `.summary` by the button's height, not to pad `.app`.

Deep links (Task 3):

- [ ] The draft message in chat carries «Відкрити в Коморі»; tapping it opens the Mini
      App **on that basket**, not on compose.
- [ ] The same link after the basket was sent, or cancelled, says
      «Чернетка вже неактуальна» rather than opening it. The two refusals share one
      sentence and differ only in the toast, which the app dropped until 2026-08-26 —
      so check the toast, not just the page.
- [ ] A link edited to another id — `?startapp=basket_<someone else's>` — says
      «Ця чернетка недоступна». This one is worth doing by hand: it is the only check
      that the launch payload buys no authority, and the whole surface rests on it.
- [ ] Launching from the menu button (no payload) still opens on compose.

The error paths, which are the ones a happy-path run never touches:

- [ ] Aeroplane mode on the sync sheet → the sheet stays, and the message says what
      landed is unknown rather than claiming nothing was sent.
- [ ] Re-confirming after that lands the cart exactly once.

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
- [x] An edit **after** the basket reached Silpo — «заміни ковбаски на салямі» →
      the sheet said «Приберемо з кошика: Ковбаски Глобино Салямки…» → «Додати в
      кошик» → the sausage left the real cart, the milk was untouched, and the
      salami arrived as 0,15 кг, one `step` of a weighted good. Verified
      2026-08-18 by reading the cart back: two lines, the replaced product absent,
      and the recap recorded both the add and the removal for the next turn.
- [x] «Яке грузинське вино є до 500 ₴?» → a free-form answer, no basket. Passes on
      `gemini-3.1-flash-lite` (3/3 in a harness against the shipped prompt and tools);
      fails on `gemma4:12b`, which answers without searching.
- [x] Clear the user's `silpo_tokens` row → any message → the re-auth prompt appears.
      Verified 2026-08-18: «купи хліб» → «Потрібно наново підключити акаунт Сільпо» →
      **Підключити Сільпо** → the authorize URL arrived with PKCE `S256` and a
      loopback `redirect_uri` → sign-in → «Готово — акаунт Сільпо підключено», about
      five seconds end to end. The same message then built a basket normally.

The login link opens on **the machine running the bot**. `KOMORA_PUBLIC_BASE_URL` is
`http://localhost:8000` by default, and Silpo accepts a loopback redirect (registered
as a `native` client per RFC 8252), so no tunnel is needed — but `localhost` on a phone
is the phone. Testing from another device is the one case that needs `cloudflared`.

### Known issues

Found by the live runs on 2026-08-11/12, and left open deliberately.

- **There is no dietary-restriction filtering.** The pass was removed rather than
  shipped wrong. It matched restriction terms as substrings, which fails in the
  dangerous direction on inflected Ukrainian: «горіхи» did **not** drop «горіх
  волоський», and «яйця» did not drop «яйце куряче», while «мед» wrongly dropped
  «медальйони». A partial allergen filter is worse than none, because it invites
  trust it has not earned. `same_word` (`passes/words.py`) fixes the inflection
  cases but would then drop «молоко без лактози» for a «лактоза» restriction —
  neither matcher understands negation. `silpo_get_my_food_restrictions` has never
  returned a populated response, so the shape of a real term is still unknown;
  `smoke_e2e.py` keeps capturing it. Rebuild this against one real payload, not
  against a guess.

- **A local model produces weaker baskets than the pipeline can rescue.** `gemma4:12b`
  emitted a line described as «Печиво або цукерки (до чаю)» — a compound phrase that
  matches no product, so it resolved to nothing and the user got «Не знайшлося». It
  also titled baskets «Завтрак» (Russian) and «Базові продукти</div>». The stray markup
  is now stripped at the model boundary; the language leak is not fixable from here.
  Both are consistent with
  [docs/local-models-ollama-gemma.md](../docs/local-models-ollama-gemma.md): local is
  for development, and the promotion gate has not been met.
- **Quantity defaults are the model's guess.** Asked for "milk and eggs" it chose three
  packs of eggs. Weighted goods are handled deterministically now — an unqualified
  amount becomes one `step`, so parmesan arrives as 100 g rather than a kilo — but for
  countable items the number is still whatever the model chose. Correcting it in plain
  language works; nothing anchors the *first* guess to what the household actually
  buys, which is what the habits engine (Plan 3) is for.
- **Product choice is four defences deep, and still not certain.** A vague query once
  returned an ice-cream cone for «основа для піци». Now: the model names a Silpo
  *category*, which **narrows** the search rather than replacing it; a verification pass
  re-reads every pick against what was asked **and against what the basket is for**,
  then re-searches the mismatches; and «⇄ N» offers the next candidate per line. The
  basket's purpose was one missing piece — judged alone, a dry-cured snack salami is a
  fine answer to «ковбаса салямі», and only «Інгредієнти для піци пепероні» makes it
  obviously wrong. **A wrongly named category is the live failure mode**, measured
  2026-08-12: «пармезан» under «Крафтові сири» returns three artisan cheeses and no
  parmesan, while the plain search returns thirty genuine Parmigiano Reggianos. Three
  things now blunt it — the candidate list never discards the search, so «⇄» always
  reaches them; the verification retry drops the category, since a rejected pick is
  evidence the shelf was part of the mistake; and the shelf only leads when it came
  back complete. The *first* pick can still be wrong when the model names a plausible
  wrong aisle, and nothing deterministic can tell that from a right one.
- **Removals are matched lexically.** «Прибери ковбаски» is matched to synced lines by
  shared word stems, so a request whose words do not land removes nothing and says so
  rather than guessing. Deliberately strict in that direction: a false positive deletes
  food from a real cart, a false negative costs two taps in the Silpo app.
- **The free-form question path needs a frontier model.** Asked «яке грузинське вино є
  до 500 ₴?», three runs each on the same question and tools:
  `gemini-3.1-flash-lite` answered correctly 3/3; `gemma4:12b` never searched, 0/3, and
  said so in fluent apologetic Ukrainian that reads like a real answer. No model reached
  for `get_products(toPrice=…)`, the parameter that actually filters by price. See
  [local models §3.3](../docs/local-models-ollama-gemma.md).
- **The Gemini free tier runs out.** `gemini-3.6-flash` returned `429
  RESOURCE_EXHAUSTED` during testing. The bot degrades to «модель недоступна», which is
  honest but unhelpful; a paid key or a retry-with-backoff would fix it. Quota is per
  (project, model), so the two tiers are pointed at two different models and a basket's
  two requests draw on two independent daily allowances — see
  verified external facts §3 (kept outside this repo).
- **A flagged line with no re-search query is dropped, not repaired.** When the
  verification pass marks a product wrong but returns an empty `better_query`,
  `pipeline._verified` has nothing to search with and the line becomes «Не знайшлося».
  Safe — a wrong product never survives — but a line that could have been recovered is
  lost. Measured at 0/10 on `gemini-3.1-flash-lite` and 4/10 on `gemini-3.5-flash-lite`,
  which is why the verification tier is the former. Falling back to the line's own
  category would fix it properly.
- **The agent never re-plans a line that fails to resolve.** A description that matches
  nothing is reported, not retried with a simpler term. Cheap to add, but it belongs
  with the other intents in Plan 4.
- **A fresh OAuth provider per message** costs two discovery requests. Caching it would
  serve stale tokens immediately after linking.
- **Overlapping quantities are replaced, not summed.** Silpo sets a quantity rather
  than adding to it, so a product already in the cart ends at whatever the draft says.
  The preview names those lines; nothing stops the user confirming.
- **Silpo's coupon endpoints are intermittently unavailable.** One run in three saw
  `get_my_coupons` fail outright. Komora degrades as designed — the cart is built, the
  coupon is listed without its enriched value — but the enrichment is best-effort.
- **Telegram is untested against the real API.** Everything below it is not.

## Model providers

Each tier is a `provider/model` ref, so switching model — or provider — is one env var.

| Ref | Key | Notes |
|---|---|---|
| `gemini/<model>` | `KOMORA_GEMINI_API_KEY` | the default for both tiers |
| `openrouter/<vendor>/<model>` | `KOMORA_OPENROUTER_API_KEY` | one key, many vendors |
| `ollama/<tag>` | none | local, development only |

**Why a third provider.** The limit Komora actually hits is Gemini's free tier, which
counts *requests* per day per (project, model) — not tokens. A basket spends one on the
proposal and one on the verification, which is why the two tiers already point at two
Gemini models. OpenRouter's allowance is not Google's, so moving **one** tier there
doubles the baskets per day without a paid key.

```bash
KOMORA_OPENROUTER_API_KEY=sk-or-v1-...
KOMORA_LLM_VERIFIER=openrouter/stealth/ox-alpha
```

Note the two slashes: `parse_model_ref` splits on the **first** one, so
`openrouter/stealth/ox-alpha` is the model `stealth/ox-alpha`. Every OpenRouter id
carries a vendor prefix, and Ollama tags already needed that rule.

`stealth/ox-alpha` is free, takes 1M tokens of context and does tool calling, which is
the only capability the agent loop needs. Two things to know before pointing a real
household's shopping at it: a **stealth** model comes from an undisclosed provider and
may change or disappear without notice, and OpenRouter's terms say that provider
**retains prompts and completions** (not for training). That is a reasonable trade for
evaluation and a deliberate decision for anything else — measure it with
`scripts/compare_models.py` before promoting it to the agent tier.

The wire format differs from the other two providers in three ways that a test pins,
because each is silent when wrong: tool arguments arrive as a JSON **string**, a tool
result is paired to its call by **`tool_call_id`** rather than by name and order, and an
upstream failure comes back as **HTTP 200 with an `error` body** — which reads as a
model that chose to say nothing if you only check the status.

## Running against a local model

Komora binds each LLM tier to a `provider/model` ref, so switching to a local Ollama model
is one env var — free, offline, no API key:

```bash
KOMORA_LLM_AGENT=ollama/gemma4:12b
KOMORA_LLM_VERIFIER=ollama/gemma4:12b
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
│   └── passes/ deterministic pipeline: resolve, promos, budget
├── db/        SQLAlchemy models and repositories
├── api/       FastAPI — OAuth callback + Mini App API (initData → JSON outcomes)
│                + serves ../web/dist at / when built
├── bot/       aiogram adapter — conversation and push
web/           the Mini App frontend (Vite + React; builds to web/dist)
```

`core/` imports no web framework and receives its dependencies as protocols, so the whole
pipeline is testable without a server, a bot, or the network.

## License

MIT — see [LICENSE](../LICENSE) at the repository root.
