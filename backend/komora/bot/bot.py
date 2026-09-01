"""The aiogram adapter — the only file in the bot package that knows about Telegram.

Handlers in `handlers.py` return a `Reply`; everything here is about getting one onto
the wire: keyboards, HTML parse mode, and Telegram's 4096-character message limit.
"""

import logging
from collections.abc import Iterator

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from komora.bot.handlers import (
    UNEXPECTED,
    Services,
    on_budget,
    on_callback,
    on_open_active,
    on_start,
    on_text,
)
from komora.bot.outcomes import Outcome, Spoke
from komora.bot.render import Reply, to_reply

log = logging.getLogger(__name__)

MAX_MESSAGE = 3900
"""Telegram's hard limit is 4096; the margin covers entity overhead."""

TERMINAL_ACTIONS = ("push", "cancel")
"""After these the original keyboard is removed, so a stale tap cannot be repeated.

«swap» is deliberately absent: it replaces the draft with a new message carrying its
own keyboard, and clearing the old one every time would litter the chat with dead
cards the user may still want to scroll back to.
"""


def make_bot(token: str) -> Bot:
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def _keyboard(reply: Reply) -> InlineKeyboardMarkup | None:
    """One button per row, except where `same_row` packs them together.

    The «⇄ N» swap controls are one per basket line, so a row each would bury «Надіслати
    в Сільпо» under a column of them.
    """
    if not reply.buttons:
        return None

    rows: list[list[InlineKeyboardButton]] = []
    for button in reply.buttons:
        cell = InlineKeyboardButton(text=button.label, callback_data=button.data, url=button.url)
        if button.same_row and rows:
            rows[-1].append(cell)
        else:
            rows.append([cell])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chunks(text: str, limit: int = MAX_MESSAGE) -> Iterator[str]:
    """Split on line boundaries. A cart with reasons on every line outgrows one
    message quickly, and splitting mid-tag would break the HTML."""
    block = ""
    for line in text.split("\n"):
        candidate = f"{block}\n{line}" if block else line
        if len(candidate) > limit and block:
            yield block
            block = line
        else:
            block = candidate
    if block:
        yield block


async def send_to(bot: Bot, chat_id: int, reply: Reply) -> None:
    """Send a reply, attaching the keyboard to the last chunk only."""
    parts = list(chunks(reply.text)) or [""]
    for part in parts[:-1]:
        await bot.send_message(chat_id, part)
    await bot.send_message(chat_id, parts[-1], reply_markup=_keyboard(reply))


async def send(message: Message, outcome: Outcome, mini_app_url: str | None = None) -> None:
    """Say an outcome. Rendering happens here, at the edge, and nowhere earlier."""
    if message.bot is None:  # only on a detached model — never inside a handler
        raise RuntimeError("message is not bound to a bot")
    await send_to(message.bot, message.chat.id, to_reply(outcome, mini_app_url))


def build_router(services: Services, mini_app_url: str | None = None) -> Router:
    """`mini_app_url` is the deployed Mini App's `t.me` link; `None` renders no deep
    link, which is what a deployment without a Mini App wants."""
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await send(message, await on_start(services, _sender(message)), mini_app_url)

    @router.message(Command("budget"))
    async def budget(message: Message, command: CommandObject) -> None:
        outcome = await on_budget(services, _sender(message), command.args or "")
        await send(message, outcome, mini_app_url)

    @router.message(Command("basket"))
    async def basket(message: Message) -> None:
        """«/basket» — the draft you have open, again.

        A draft card scrolls away, and until now the only way back to one was to
        build another — which discards it. Same reason the Mini App's menu button
        needed `GET /api/baskets/active`.
        """
        await send(message, await on_open_active(services, _sender(message)), mini_app_url)

    @router.message(F.text)
    async def text(message: Message) -> None:
        outcome = await on_text(services, _sender(message), message.text or "")
        await send(message, outcome, mini_app_url)

    @router.callback_query(F.data)
    async def callback(query: CallbackQuery) -> None:
        data = query.data or ""
        reply = to_reply(await on_callback(services, _sender(query), data), mini_app_url)
        await query.answer(reply.toast or "")

        if data.partition(":")[0] in TERMINAL_ACTIONS and isinstance(query.message, Message):
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                log.debug("could not clear the keyboard", exc_info=True)

        # A tap on a message Telegram no longer exposes still deserves an answer, so
        # the reply is sent by chat id rather than as a response to that message.
        if query.message is not None and query.bot is not None:
            await send_to(query.bot, query.message.chat.id, reply)

    return router


def _sender(event: Message | CallbackQuery) -> int:
    if event.from_user is None:  # channel posts and anonymous admins have no user
        raise ValueError("update carries no sender")
    return event.from_user.id


def build_dispatcher(services: Services, mini_app_url: str | None = None) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(services, mini_app_url))

    @dispatcher.errors()
    async def unhandled(event: ErrorEvent) -> bool:
        """The last line of defence, because silence is the worst answer available.

        Handlers translate every failure they can name; anything that gets past them
        — a malformed model payload, a bug — used to die here with nothing sent.
        """
        log.error("update failed outside every handler", exc_info=event.exception)
        message = event.update.message
        query = event.update.callback_query
        chat_id = (
            message.chat.id
            if message is not None
            else query.message.chat.id
            if query is not None and query.message is not None
            else None
        )
        if chat_id is not None and (bot := event.update.bot) is not None:
            # `deliver_unexpected` swallows its own failures — the net must not become
            # a second failure of its own — so there is nothing to catch here.
            await deliver_unexpected(bot, chat_id, query)
        return True

    return dispatcher


async def deliver_unexpected(bot: Bot, chat_id: int, query: CallbackQuery | None = None) -> None:
    """The unhandled-error notice: one honest sentence, wherever the user was.

    Never raises — the safety net must not become a second failure of its own.
    """
    try:
        if query is not None:
            await query.answer()
        await send_to(bot, chat_id, to_reply(Spoke(UNEXPECTED)))
    except Exception:
        log.debug("could not deliver the error notice", exc_info=True)
