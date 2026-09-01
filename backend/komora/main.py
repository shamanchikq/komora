"""Entrypoint: the HTTP callback and the bot in one process.

Both are needed at once. The bot uses long polling and needs no public URL, but Silpo's
OAuth callback does, so uvicorn serves exactly one route while aiogram polls.

    uv run alembic upgrade head
    uv run python -m komora.main
"""

import asyncio
import contextlib
import logging
import sys

import uvicorn
from dotenv import load_dotenv

from komora.api.app import create_app
from komora.bot.bot import build_dispatcher, make_bot
from komora.bot.handlers import Services
from komora.config import Settings
from komora.core.agent.tools import CachedTools
from komora.core.crypto import TokenCipher
from komora.core.llm.factory import make_llm
from komora.core.mcp.auth import AuthorizationBridge
from komora.core.mcp.gateway import SilpoGateway
from komora.db.base import make_engine, make_session_factory
from komora.db.migrate import SchemaOutOfDate, assert_current
from komora.db.repo import BasketRepo, ConversationRepo, OAuthClientRepo, UserRepo

log = logging.getLogger("komora")

LINK_PROMPT = "Відкрийте це посилання й увійдіть у Сільпо:"
LINK_DONE = "Готово — акаунт Сільпо підключено. Скажіть, що потрібно купити."
LINK_FAILED = "Не вдалося підключити акаунт. Спробуйте ще раз через /start."


async def run() -> None:
    load_dotenv()
    settings = Settings()

    # Ukrainian product names on a cp1252 console would raise mid-message.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    # Before anything opens a session: a stale schema must fail here, with the name of
    # the database in the message, not on the first basket write minutes later.
    assert_current(settings.database_url)

    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    users, clients = UserRepo(sessions), OAuthClientRepo(sessions)

    bridge = AuthorizationBridge()
    gateway = SilpoGateway(
        server_url=settings.silpo_mcp_url,
        public_base_url=settings.public_base_url,
        users=users,
        clients=clients,
        cipher=TokenCipher(settings.token_encryption_key),
        bridge=bridge,
    )

    bot = make_bot(settings.telegram_bot_token)

    async def send_url(telegram_id: int, url: str) -> None:
        await bot.send_message(telegram_id, f"{LINK_PROMPT}\n{url}")

    linking: dict[int, asyncio.Task[None]] = {}

    async def link(telegram_id: int) -> None:
        """Run the OAuth round-trip in the background.

        It blocks until the user finishes signing in — minutes, potentially — so it
        must never run inside a handler. One flow per user at a time; a second tap
        while the first is still open would strand the earlier one.
        """
        existing = linking.get(telegram_id)
        if existing is not None and not existing.done():
            return

        async def flow() -> None:
            try:
                await gateway.link(telegram_id, send_url)
                await bot.send_message(telegram_id, LINK_DONE)
            except Exception:
                log.exception("linking failed for %s", telegram_id)
                await bot.send_message(telegram_id, LINK_FAILED)
            finally:
                linking.pop(telegram_id, None)

        linking[telegram_id] = asyncio.create_task(flow())

    services = Services(
        users=users,
        conversations=ConversationRepo(sessions),
        baskets=BasketRepo(sessions),
        llm=make_llm(settings.llm_agent, settings),
        # This second client was configured and validated from the start and then never
        # built, so both requests in a basket went to one model and drained one daily
        # quota. Free-tier limits are per (project, model), so this is capacity as much
        # as it is model choice.
        verifier=make_llm(settings.llm_verifier, settings),
        tools=CachedTools(),
        connect=gateway.connect,
        start_linking=link,
    )

    dispatcher = build_dispatcher(services, settings.telegram_mini_app_url or None)
    # ONE process, deliberately — not merely the default.
    #
    # `AuthorizationBridge` keeps pending OAuth flows in a dict in this process, and the
    # redirect that resolves one arrives as a separate HTTP request. Under two workers
    # that request lands in the wrong process perhaps half the time, `resolve()` finds no
    # pending state, and account linking simply never completes — no error, just a user
    # waiting on a callback nobody is holding. Long polling has the same shape: two
    # pollers would fight over the same updates.
    #
    # Serving this app from `uvicorn --workers N`, gunicorn, or more than one container
    # therefore breaks linking silently. Fix it by moving the bridge's state into shared
    # storage before scaling out, not by adding workers and hoping.
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(bridge, services, settings.telegram_bot_token),
            host="0.0.0.0",
            port=settings.http_port,
            log_level="info",
            workers=1,
        )
    )

    log.info("Komora is up: callback on :%s, bot polling", settings.http_port)
    try:
        await asyncio.gather(server.serve(), dispatcher.start_polling(bot))
    finally:
        server.should_exit = True
        for task in linking.values():
            task.cancel()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run())
    except SchemaOutOfDate as exc:
        # A traceback would bury the one line that says what to do.
        raise SystemExit(f"\nDatabase schema is out of date.\n\n{exc}\n") from None


if __name__ == "__main__":
    main()
