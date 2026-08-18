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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from komora.bot.handlers import Services, on_budget, on_callback, on_start, on_text
from komora.bot.outcomes import Outcome
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


async def send(message: Message, outcome: Outcome) -> None:
    """Say an outcome. Rendering happens here, at the edge, and nowhere earlier."""
    if message.bot is None:  # only on a detached model — never inside a handler
        raise RuntimeError("message is not bound to a bot")
    await send_to(message.bot, message.chat.id, to_reply(outcome))


def build_router(services: Services) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await send(message, await on_start(services, _sender(message)))

    @router.message(Command("budget"))
    async def budget(message: Message, command: CommandObject) -> None:
        await send(message, await on_budget(services, _sender(message), command.args or ""))

    @router.message(F.text)
    async def text(message: Message) -> None:
        await send(message, await on_text(services, _sender(message), message.text or ""))

    @router.callback_query(F.data)
    async def callback(query: CallbackQuery) -> None:
        data = query.data or ""
        reply = to_reply(await on_callback(services, _sender(query), data))
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


def build_dispatcher(services: Services) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(services))
    return dispatcher
