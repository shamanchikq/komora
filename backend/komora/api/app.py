"""HTTP surface.

Deliberately minimal: the bot runs on long polling, so the only endpoint that must be
publicly reachable is Silpo's OAuth callback.

There is no `/auth/silpo/start/{telegram_id}`. Linking is initiated by the bot, which
already knows who is asking; an unauthenticated endpoint accepting a telegram_id would
let anyone start a flow on anyone's behalf.

**Single process, by design.** The callback resolves a flow held in this process's
`AuthorizationBridge`. Run this app under more than one worker and roughly half the
callbacks land where nothing is waiting — linking then fails silently rather than
loudly. See the comment at the `uvicorn.Config` in `main.py`.

**Anything added here authenticates on its own.** Every endpoint below is public by
necessity: the callback is reached by Silpo's redirect, not by a user Komora can
identify. The bot's guarantees do not extend here — in particular the ownership check
in `bot/handlers.on_callback` (`basket.user_id != telegram_id`), which is the only
thing stopping one user from acting on another's basket. A Mini App surface must verify
Telegram `initData` (HMAC over the bot token) and re-apply that check per request; it
cannot inherit either.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from komora.core.mcp.auth import AuthorizationBridge

_PAGE = """<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Комора</title>
<style>
  body{{margin:0;min-height:100vh;display:grid;place-items:center;
       background:#FCFAF6;color:#1E1A15;
       font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
  main{{max-width:22rem;padding:2rem;text-align:center}}
  h1{{font-size:1.25rem;margin:0 0 .5rem}}
  p{{color:#8B8175;margin:0}}
  @media (prefers-color-scheme:dark){{
    body{{background:#17130F;color:#F5EFE7}} p{{color:#9C9084}}
  }}
</style></head>
<body><main><h1>{title}</h1><p>{message}</p></main></body></html>
"""


def create_app(bridge: AuthorizationBridge) -> FastAPI:
    app = FastAPI(title="Komora", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/auth/silpo/callback", response_class=HTMLResponse)
    async def silpo_callback(
        state: str = Query(...),
        code: str | None = Query(default=None),
        iss: str | None = Query(default=None),
        error: str | None = Query(default=None),
        error_description: str | None = Query(default=None),
    ) -> HTMLResponse:
        """Hand the authorization result to the coroutine waiting on `state`.

        `iss` is forwarded because the SDK validates it per RFC 9207.
        """
        if error is not None:
            if not bridge.fail(state, error_description or error):
                raise HTTPException(status_code=400, detail="Unknown or expired authorization.")
            return HTMLResponse(
                _PAGE.format(
                    title="Доступ не надано",
                    message="Комора не отримала доступу до вашого акаунта Сільпо. "
                    "Можете спробувати ще раз у чаті.",
                )
            )

        if code is None:
            raise HTTPException(status_code=422, detail="Missing 'code'.")

        if not bridge.resolve(state, code=code, iss=iss):
            raise HTTPException(status_code=400, detail="Unknown or expired authorization.")

        return HTMLResponse(
            _PAGE.format(title="Готово", message="Поверніться в Telegram — Комора вже працює.")
        )

    return app
