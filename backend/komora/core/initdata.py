"""Telegram Mini App identity: verify `initData` and map it to a user.

Telegram is the only identity provider (spec §8). The Telegram client hands the Mini
App an `initData` payload at launch; the app sends it with every request, and this
module is the server-side half of that handshake — the same check Telegram's own
documentation prescribes for a validating backend:

    secret_key  = HMAC-SHA256(key=b"WebAppData",       msg=bot_token)
    hash        = HMAC-SHA256(key=secret_key,          msg=data_check_string)

where `data_check_string` is every received field except `hash` itself, sorted by key
and joined as `key=value` lines. Compared against the `hash` field in constant time.

HMAC rather than the Ed25519 third-party signature is deliberate: Komora owns the bot,
so the token is already inside the process and there is no third party to convince.

**The payload is frozen at launch** — a Mini App left open all evening sends the same
`auth_date` on every request — so the freshness window must outlast a realistic session,
not a single request. Within it a replayed payload is indistinguishable from its owner;
over HTTPS, with the payload held only by the client that was handed it, that is the
accepted exposure.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_S = 86_400
"""One day: longer than any cart review, short enough to matter."""


class InitDataRejected(Exception):
    """The payload failed verification — malformed, forged, or stale."""


def verify_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_s: int = MAX_AGE_S,
    now: int | None = None,
) -> int:
    """Return the telegram_id of whoever launched the Mini App.

    Raises `InitDataRejected` with the reason otherwise. `now` exists for tests; every
    caller in production takes the wall clock.
    """
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received = fields.pop("hash", None)
    if not received:
        raise InitDataRejected("missing hash")

    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received):
        raise InitDataRejected("signature mismatch")

    try:
        auth_date = int(fields["auth_date"])
    except KeyError, ValueError:
        raise InitDataRejected("missing or malformed auth_date") from None

    moment = int(time.time()) if now is None else now
    if abs(moment - auth_date) > max_age_s:
        # Absolute: a client clock skewed into the future is as suspect as a stale one.
        raise InitDataRejected("stale auth_date")

    try:
        user = json.loads(fields["user"])
        telegram_id = int(user["id"])
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        raise InitDataRejected("no usable user field") from None
    if telegram_id <= 0:
        raise InitDataRejected("no usable user field")
    return telegram_id
