"""Mint a launch URL that opens the Mini App authenticated, in an ordinary browser.

**Development only.** Telegram normally hands the Mini App its `initData` at launch,
which means trying the app at all needs a published Web App (BotFather), an HTTPS URL
(so a tunnel), and a phone. That is a long way to go to look at a screen — and none of
it is needed to exercise the frontend against the real backend.

Telegram passes the launch payload in the URL **fragment**, and the vendored
`telegram-web-app.js` parses it from there. So a payload signed with the bot token,
put in the fragment, is indistinguishable from a real launch to everything downstream:
`api/minapp.py` verifies the same HMAC, `_own_draft` applies the same ownership rules,
and the basket is a real basket in the real database.

**`tgWebAppPlatform` is deliberately omitted.** Setting it would make `telegramHost()`
report a Telegram client, the app would hand its primary action to the native
MainButton, and no Telegram client exists to draw one — leaving a page with no
controls. Left unset, `platform` stays "unknown", the in-page fallback bar renders,
and everything else behaves identically.

This grants whoever holds the URL the same access the Telegram user has, for
`initdata.MAX_AGE_S` (24 h). It is a credential: do not paste it anywhere shared, and
do not point this at a deployment.

USAGE
    uv run python scripts/dev_miniapp_url.py --user 514182045
    uv run python scripts/dev_miniapp_url.py --user 514182045 --basket 42   # deep link
"""

import argparse
import hashlib
import hmac
import json
import pathlib
import sys
import time
import urllib.parse

from dotenv import load_dotenv

from komora.config import Settings

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def sign(fields: dict[str, str], bot_token: str) -> str:
    """Telegram's documented scheme, the same one `core/initdata.py` verifies."""
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    signed = {
        **fields,
        "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(),
    }
    return urllib.parse.urlencode(signed)


def main(args: argparse.Namespace) -> int:
    load_dotenv(ENV_FILE)
    settings = Settings(_env_file=str(ENV_FILE))

    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdev",
        "user": json.dumps(
            {"id": args.user, "first_name": "Dev", "language_code": "uk"},
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    }
    if args.basket is not None:
        # What a «Відкрити в Коморі» button puts in `?startapp=` — the app reads it
        # once, before first paint, and opens on that basket instead of compose.
        fields["start_param"] = f"basket_{args.basket}"

    payload = urllib.parse.quote(sign(fields, settings.telegram_bot_token), safe="")
    print(f"{args.base.rstrip('/')}/#tgWebAppData={payload}")
    print(
        f"\n  user {args.user} · valid ~24h · dev only, this URL is a credential",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", type=int, required=True, help="telegram_id to launch as")
    parser.add_argument("--basket", type=int, help="open on this basket, as a deep link would")
    parser.add_argument("--base", default="http://localhost:8000", help="where the app is served")
    sys.exit(main(parser.parse_args()))
